"""Site público (cliente final, sem login).

Regras de privacidade:
- Pela placa, qualquer um vê SÓ serviço, data e situação das consultas feitas no site público.
- O resultado (código de segurança, PDF) só abre com o token de acesso do comprador
  (salvo no aparelho e enviado por e-mail). De outro aparelho: reenvio para o e-mail da compra.
"""

import logging
import mimetypes

from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404
from rest_framework import serializers
from rest_framework.decorators import api_view, authentication_classes, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle

from . import servicos
from .emails import enviar_links_por_placa
from .integracao import infinitepay
from .models import Cobranca, Pedido, Servico
from .serializers import DadosServicoMixin
from .validacoes import so_digitos, telefone_valido

log = logging.getLogger("core.publico")


class _PorIP(SimpleRateThrottle):
    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class ThrottleLeitura(_PorIP):
    scope = "publico_leitura"


class ThrottleAcao(_PorIP):
    scope = "publico"


def publico(*metodos, acao=False):
    """Decorador: rota sem login, sem autenticação e com limite por IP."""
    def decorar(f):
        f = throttle_classes([ThrottleAcao if acao else ThrottleLeitura])(f)
        f = permission_classes([AllowAny])(f)
        f = authentication_classes([])(f)
        return api_view(list(metodos))(f)
    return decorar


def _placa(valor):
    try:
        return servicos.normalizar_placa(valor)
    except ValueError as exc:
        raise serializers.ValidationError({"placa": [str(exc)]}) from exc


# --- Catálogo e histórico -----------------------------------------------------

@publico("GET")
def servicos_publicos(request):
    qs = Servico.objects.filter(publico_ativo=True, preco_publico_centavos__gte=100)
    return Response([
        {"codigo": s.codigo, "nome": s.nome, "descricao": s.descricao, "preco_centavos": s.preco_publico_centavos,
         "assincrono": s.assincrono, "formulario": s.formulario}
        for s in qs
    ])


@publico("GET")
def historico_placa(request, placa):
    placa = _placa(placa)
    qs = Pedido.objects.filter(origem=Pedido.Origem.PUBLICO, placa=placa).select_related("servico")[:20]
    return Response({
        "placa": placa,
        "consultas": [
            {
                "servico": p.servico.nome,
                "servico_codigo": p.servico.codigo,
                "status": p.status,
                "status_display": p.get_status_display(),
                "criado_em": p.criado_em,
                "atualizado_em": p.atualizado_em,
                "reembolso_pendente": p.reembolso_pendente,
                "ref": p.id.hex[:8].upper(),
            }
            for p in qs
        ],
    })


MSG_LINKS = "Se houver consultas desta placa feitas com este e-mail, enviamos os links de acesso em instantes."


@publico("POST", acao=True)
def reenviar_links(request, placa):
    placa = _placa(placa)
    email = str(request.data.get("email", "")).strip().lower()
    if email:
        pedidos = list(Pedido.objects.filter(origem=Pedido.Origem.PUBLICO, placa=placa, comprador_email__iexact=email)
                       .exclude(token_acesso=None).select_related("servico")[:20])
        if pedidos:
            try:
                enviar_links_por_placa(email, pedidos)
            except Exception:  # noqa: BLE001
                log.exception("Falha ao reenviar links da placa %s", placa)
    return Response({"detail": MSG_LINKS})


# --- Pagamento ------------------------------------------------------------------

class PagarSerializer(DadosServicoMixin, serializers.Serializer):
    dados = serializers.JSONField(required=False, write_only=True)
    servico = serializers.SlugRelatedField(slug_field="codigo", queryset=Servico.objects.filter(publico_ativo=True))
    placa = serializers.CharField(max_length=10)
    nome = serializers.CharField(min_length=3, max_length=150)
    email = serializers.EmailField(max_length=254)
    telefone = serializers.CharField(max_length=20)
    aceite = serializers.BooleanField()

    def validate_placa(self, v):
        return _placa(v)

    def validate_nome(self, v):
        v = " ".join(v.split())
        if len(v.split(" ")) < 2:
            raise serializers.ValidationError("Informe nome e sobrenome.")
        return v

    def validate_email(self, v):
        return v.strip().lower()

    def validate_telefone(self, v):
        if not telefone_valido(v):
            raise serializers.ValidationError("Telefone inválido. Use DDD + número.")
        return so_digitos(v)

    def validate_aceite(self, v):
        if not v:
            raise serializers.ValidationError("É preciso aceitar os termos de uso.")
        return v


@publico("POST", acao=True)
def pagar(request):
    if not infinitepay.configurado():
        return Response({"detail": "Pagamento indisponível no momento. Tente novamente mais tarde."}, status=503)
    ser = PagarSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    d = ser.validated_data
    try:
        c = servicos.iniciar_checkout_publico(d["servico"], d["placa"], d["nome"], d["email"], d["telefone"], dados=d.get("dados"))
    except ValueError as exc:
        return Response({"detail": str(exc)}, status=400)
    except infinitepay.ErroCheckout as exc:
        return Response({"detail": str(exc)}, status=502)
    return Response({"cobranca": str(c.id), "token": c.token_acesso, "url": c.link_url}, status=201)


@publico("POST")
def confirmar(request, pk):
    c = get_object_or_404(Cobranca, pk=pk, tipo=Cobranca.Tipo.PUBLICO)
    if not c.token_acesso or str(request.data.get("t", "")) != c.token_acesso:
        raise Http404
    c = servicos.confirmar_cobranca(
        c.pk, transaction_nsu=str(request.data.get("transaction_nsu", ""))[:80],
        slug=str(request.data.get("slug", ""))[:80],
    )
    return Response({
        "status": c.status,
        "pedido": c.token_acesso if c.pedido_id else None,
        "mensagem": c.mensagem,
        "url": c.link_url if c.status == Cobranca.Status.PENDENTE else None,
        "valor_centavos": c.valor_centavos,
    })


# --- Resultado pelo token ---------------------------------------------------------

def _pedido(token):
    if not token or len(token) < 20:
        raise Http404
    return get_object_or_404(Pedido.objects.select_related("servico"), token_acesso=token, origem=Pedido.Origem.PUBLICO)


def _pedido_json(p):
    return {
        "servico": p.servico.nome,
        "servico_codigo": p.servico.codigo,
        "assincrono": p.servico.assincrono,
        "placa": p.placa,
        "status": p.status,
        "status_display": p.get_status_display(),
        "mensagem": p.mensagem,
        "codigo_seguranca": p.destaque,
        "resultado": p.resultado if p.status == Pedido.Status.SUCESSO else None,
        "tem_documento": bool(p.documento),
        "valor_centavos": p.valor_centavos,
        "reembolso_pendente": p.reembolso_pendente,
        "reembolsado": bool(p.reembolsado_em),
        "criado_em": p.criado_em,
        "atualizado_em": p.atualizado_em,
        "ref": p.id.hex[:8].upper(),
    }


@publico("GET")
def pedido(request, token):
    return Response(_pedido_json(_pedido(token)))


@publico("POST")
def pedido_atualizar(request, token):
    return Response(_pedido_json(servicos.sincronizar_pedido(_pedido(token))))


@publico("GET")
def pedido_documento(request, token):
    p = _pedido(token)
    if not p.documento:
        raise Http404
    ext = p.documento.name.rsplit(".", 1)[-1]
    tipo = mimetypes.guess_type(p.documento.name)[0] or "application/octet-stream"
    return FileResponse(p.documento.open("rb"), content_type=tipo, as_attachment=True,
                        filename=f"{p.servico.codigo.upper()}-{p.placa}.{ext}")
