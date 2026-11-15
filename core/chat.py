"""Chat interno de suporte: cliente <-> equipe (atualização por consulta periódica)."""

import mimetypes
import secrets

from django.db import transaction
from django.db.models import Count, F, Q
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.decorators import (
    api_view, authentication_classes, parser_classes, permission_classes, throttle_classes,
)
from rest_framework.permissions import AllowAny
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle

from .models import Conversa, Mensagem, Pedido
from .publico import ThrottleAcao, ThrottleLeitura

MAX_ANEXO = 5 * 1024 * 1024
EXTENSOES = (".pdf", ".png", ".jpg", ".jpeg", ".webp")


class ThrottleChat(UserRateThrottle):
    """Limita só o ENVIO de mensagens; a consulta periódica (GET) é livre."""

    scope = "chat"

    def allow_request(self, request, view):
        if request.method != "POST":
            return True
        return super().allow_request(request, view)


# --- Consultas ----------------------------------------------------------------

def _conversas_visiveis(usuario):
    qs = Conversa.objects.select_related("cliente", "pedido__servico")
    return qs if usuario.is_staff else qs.filter(cliente=usuario)


def _com_nao_lidas(qs, usuario):
    if usuario.is_staff:
        filtro = Q(mensagens__da_equipe=False) & (
            Q(lido_equipe_em__isnull=True) | Q(mensagens__criado_em__gt=F("lido_equipe_em"))
        )
    else:
        filtro = Q(mensagens__da_equipe=True) & (
            Q(lido_cliente_em__isnull=True) | Q(mensagens__criado_em__gt=F("lido_cliente_em"))
        )
    return qs.annotate(nao_lidas=Count("mensagens", filter=filtro))


def _conversa(usuario, pk):
    return get_object_or_404(_conversas_visiveis(usuario), pk=pk)


# --- Serialização -------------------------------------------------------------

def _mensagem_json(m, usuario):
    if usuario is None:  # visitante do site público
        return {
            "id": m.id, "texto": m.texto, "da_equipe": m.da_equipe, "minha": not m.da_equipe,
            "autor": ("Equipe" + (f" · {m.autor.first_name.split(' ')[0]}" if m.autor and m.autor.first_name else ""))
            if m.da_equipe else "Você",
            "criado_em": m.criado_em, "anexo": None,
        }
    autor = "Você" if m.autor_id == usuario.id else (
        ("Equipe" + (f" · {m.autor.first_name.split(' ')[0]}" if m.autor and m.autor.first_name else ""))
        if m.da_equipe else ((m.autor.first_name or m.autor.username) if m.autor
                             else (m.conversa.visitante_nome or "Visitante"))
    )
    return {
        "id": m.id,
        "texto": m.texto,
        "da_equipe": m.da_equipe,
        "minha": m.autor_id == usuario.id,
        "autor": autor,
        "criado_em": m.criado_em,
        "anexo": {"nome": m.anexo_nome or "anexo", "url": f"/chat/anexos/{m.id}/"} if m.anexo else None,
    }


def _conversa_json(c, usuario, ultima=None):
    dados = {
        "id": c.id,
        "assunto": c.assunto,
        "status": c.status,
        "status_display": c.get_status_display(),
        "ultima_mensagem_em": c.ultima_mensagem_em,
        "nao_lidas": getattr(c, "nao_lidas", 0),
        "pedido": {"id": str(c.pedido_id), "placa": c.pedido.placa, "servico": c.pedido.servico.nome} if c.pedido else None,
        "ultima": None,
    }
    if ultima:
        dados["ultima"] = {
            "texto": ultima.texto[:120] or ("📎 " + (ultima.anexo_nome or "anexo")),
            "da_equipe": ultima.da_equipe,
        }
    if usuario is not None and usuario.is_staff:
        u = c.cliente
        dados["cliente"] = {
            "id": u.id, "nome": u.first_name or u.username, "usuario": u.username, "documento": u.documento,
            "telefone": u.telefone, "saldo_centavos": u.saldo_centavos, "visitante": False,
        } if u else {
            "id": None, "nome": c.visitante_nome or "Visitante", "usuario": "visitante do site",
            "documento": "", "telefone": c.visitante_contato, "saldo_centavos": None, "visitante": True,
        }
    return dados


class NovaMensagemSerializer(serializers.Serializer):
    texto = serializers.CharField(max_length=2000, required=False, allow_blank=True, trim_whitespace=True)
    anexo = serializers.FileField(required=False)

    def validate_anexo(self, a):
        if a.size > MAX_ANEXO:
            raise serializers.ValidationError("Arquivo muito grande (máx. 5 MB).")
        if not a.name.lower().endswith(EXTENSOES):
            raise serializers.ValidationError("Envie PDF ou imagem (PNG, JPG, WEBP).")
        return a

    def validate(self, d):
        if not d.get("texto") and not d.get("anexo"):
            raise serializers.ValidationError({"texto": "Escreva uma mensagem ou anexe um arquivo."})
        return d


class NovaConversaSerializer(NovaMensagemSerializer):
    assunto = serializers.CharField(max_length=120, required=False, allow_blank=True)
    pedido = serializers.UUIDField(required=False, allow_null=True)


def _criar_mensagem(conversa, usuario, dados):
    anexo = dados.get("anexo")
    m = Mensagem.objects.create(
        conversa=conversa,
        autor=usuario,
        da_equipe=usuario.is_staff,
        texto=dados.get("texto", ""),
        anexo=anexo,
        anexo_nome=(anexo.name[:120] if anexo else ""),
    )
    agora = m.criado_em
    campos = {"ultima_mensagem_em": agora}
    # quem envia já leu tudo até aqui
    campos["lido_equipe_em" if usuario.is_staff else "lido_cliente_em"] = agora
    if not usuario.is_staff and conversa.status == Conversa.Status.RESOLVIDA:
        campos["status"] = Conversa.Status.ABERTA  # cliente voltou a falar: reabre
    Conversa.objects.filter(pk=conversa.pk).update(**campos)
    return m


# --- Views --------------------------------------------------------------------

@api_view(["GET", "POST"])
@parser_classes([MultiPartParser, FormParser, JSONParser])
@throttle_classes([ThrottleChat])
def conversas(request):
    u = request.user
    if request.method == "POST":
        return _nova_conversa(request)

    qs = _com_nao_lidas(_conversas_visiveis(u), u)
    if s := request.query_params.get("status"):
        qs = qs.filter(status=s)
    if u.is_staff and (b := request.query_params.get("busca", "").strip()):
        qs = qs.filter(
            Q(cliente__first_name__icontains=b) | Q(cliente__username__icontains=b)
            | Q(visitante_nome__icontains=b) | Q(visitante_contato__icontains=b)
            | Q(cliente__documento__icontains=b) | Q(assunto__icontains=b) | Q(pedido__placa__icontains=b.upper())
        )
    lista = list(qs[:100])
    ultimas = {}
    for m in Mensagem.objects.filter(conversa__in=lista).order_by("conversa_id", "-criado_em", "-id"):
        ultimas.setdefault(m.conversa_id, m)
    return Response([_conversa_json(c, u, ultimas.get(c.id)) for c in lista])


def _nova_conversa(request):
    u = request.user
    if u.is_staff:
        return Response({"detail": "A equipe responde às conversas abertas pelos clientes."}, status=400)
    ser = NovaConversaSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    d = ser.validated_data
    pedido = None
    if d.get("pedido"):
        pedido = Pedido.objects.filter(pk=d["pedido"], usuario=u).select_related("servico").first()
        if not pedido:
            return Response({"pedido": ["Pedido não encontrado."]}, status=400)
    assunto = (d.get("assunto") or "").strip() or (
        f"{pedido.servico.nome} · {pedido.placa}" if pedido else (d.get("texto") or "Nova conversa")[:60]
    )
    with transaction.atomic():
        conversa = Conversa.objects.create(cliente=u, assunto=assunto[:120], pedido=pedido)
        _criar_mensagem(conversa, u, d)
    conversa = _com_nao_lidas(_conversas_visiveis(u), u).get(pk=conversa.pk)
    return Response(_conversa_json(conversa, u), status=status.HTTP_201_CREATED)


@api_view(["GET", "POST"])
@parser_classes([MultiPartParser, FormParser, JSONParser])
@throttle_classes([ThrottleChat])
def mensagens(request, pk):
    u = request.user
    conversa = _conversa(u, pk)
    if request.method == "POST":
        ser = NovaMensagemSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        m = _criar_mensagem(conversa, u, ser.validated_data)
        return Response(_mensagem_json(m, u), status=status.HTTP_201_CREATED)

    qs = conversa.mensagens.select_related("autor")
    depois = request.query_params.get("depois")
    if depois and depois.isdigit():
        qs = qs.filter(id__gt=int(depois))
    else:
        ids = list(conversa.mensagens.order_by("-id").values_list("id", flat=True)[:200])
        qs = qs.filter(id__in=ids)
    lista = list(qs)
    # marcar como lido do lado de quem está vendo
    Conversa.objects.filter(pk=conversa.pk).update(
        **{"lido_equipe_em" if u.is_staff else "lido_cliente_em": timezone.now()}
    )
    conversa.refresh_from_db()
    return Response({
        "conversa": _conversa_json(conversa, u),
        "mensagens": [_mensagem_json(m, u) for m in lista],
    })


@api_view(["POST"])
def alterar_status(request, pk):
    u = request.user
    conversa = _conversa(u, pk)
    novo = request.data.get("status")
    if novo not in Conversa.Status.values:
        return Response({"status": ["Status inválido."]}, status=400)
    conversa.status = novo
    conversa.save(update_fields=["status"])
    return Response({"status": novo})


@api_view(["GET"])
def nao_lidas(request):
    u = request.user
    qs = _com_nao_lidas(_conversas_visiveis(u), u)
    if u.is_staff:
        qs = qs.filter(status=Conversa.Status.ABERTA)
    return Response({"total": sum(c.nao_lidas for c in qs)})


@api_view(["GET"])
def anexo(request, pk):
    m = get_object_or_404(Mensagem.objects.select_related("conversa"), pk=pk)
    if not request.user.is_staff and m.conversa.cliente_id != request.user.id:
        raise Http404
    if not m.anexo:
        raise Http404
    tipo = mimetypes.guess_type(m.anexo.name)[0] or "application/octet-stream"
    return FileResponse(m.anexo.open("rb"), content_type=tipo, as_attachment=True, filename=m.anexo_nome or "anexo")



# --- Visitante do site público (sem login) ---------------------------------------



class ThrottleChatVisitante(ThrottleLeitura):
    """Consulta livre (limite de leitura); envio limitado como ação pública."""

    def allow_request(self, request, view):
        if request.method == "POST":
            acao = ThrottleAcao()
            return acao.allow_request(request, view)
        return super().allow_request(request, view)


class NovaConversaVisitante(serializers.Serializer):
    nome = serializers.CharField(min_length=2, max_length=120)
    contato = serializers.CharField(min_length=5, max_length=150, help_text="E-mail ou WhatsApp")
    texto = serializers.CharField(max_length=2000)
    placa = serializers.CharField(max_length=10, required=False, allow_blank=True)


def _conversa_visitante(token):
    if not token or len(token) < 20:
        raise Http404
    return get_object_or_404(Conversa, visitante_token=token)


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([ThrottleChatVisitante])
def visitante_iniciar(request):
    ser = NovaConversaVisitante(data=request.data)
    ser.is_valid(raise_exception=True)
    d = ser.validated_data
    placa = "".join(ch for ch in (d.get("placa") or "").upper() if ch.isalnum())[:7]
    assunto = f"Site · {placa}" if placa else f"Site · {d['texto'][:50]}"
    with transaction.atomic():
        conversa = Conversa.objects.create(
            visitante_nome=d["nome"].strip(), visitante_contato=d["contato"].strip(), assunto=assunto[:120],
            visitante_token=secrets.token_urlsafe(32),
        )
        m = Mensagem.objects.create(conversa=conversa, texto=d["texto"].strip())
        Conversa.objects.filter(pk=conversa.pk).update(ultima_mensagem_em=m.criado_em, lido_cliente_em=m.criado_em)
    return Response({"token": conversa.visitante_token, "nome": conversa.visitante_nome}, status=201)


@api_view(["GET", "POST"])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([ThrottleChatVisitante])
def visitante_mensagens(request, token):
    conversa = _conversa_visitante(token)
    if request.method == "POST":
        texto = str(request.data.get("texto", "")).strip()
        if not texto or len(texto) > 2000:
            return Response({"texto": ["Escreva uma mensagem (até 2000 caracteres)."]}, status=400)
        m = Mensagem.objects.create(conversa=conversa, texto=texto)
        campos = {"ultima_mensagem_em": m.criado_em, "lido_cliente_em": m.criado_em}
        if conversa.status == Conversa.Status.RESOLVIDA:
            campos["status"] = Conversa.Status.ABERTA
        Conversa.objects.filter(pk=conversa.pk).update(**campos)
        return Response(_mensagem_json(m, None), status=201)

    qs = conversa.mensagens.select_related("autor")
    depois = request.query_params.get("depois")
    if depois and depois.isdigit():
        qs = qs.filter(id__gt=int(depois))
    else:
        ids = list(conversa.mensagens.order_by("-id").values_list("id", flat=True)[:200])
        qs = qs.filter(id__in=ids)
    lista = list(qs)
    Conversa.objects.filter(pk=conversa.pk).update(lido_cliente_em=timezone.now())
    return Response({
        "conversa": {"status": conversa.status, "nome": conversa.visitante_nome},
        "mensagens": [_mensagem_json(m, None) for m in lista],
    })


@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([ThrottleLeitura])
def visitante_nao_lidas(request, token):
    c = _conversa_visitante(token)
    qs = c.mensagens.filter(da_equipe=True)
    if c.lido_cliente_em:
        qs = qs.filter(criado_em__gt=c.lido_cliente_em)
    return Response({"total": qs.count()})
