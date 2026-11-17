import json
import logging
import mimetypes

from django.conf import settings
from django.db.models import Count, Q, Sum
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from rest_framework import generics, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle

from . import servicos
from .integracao.cliente import ClienteFonte, ErroFonte, assinatura_valida, extrair_protocolo
from .models import Cobranca, Movimentacao, Pedido, Servico, Usuario
from .permissoes import EhAdmin
from .serializers import (
    MovimentacaoSerializer,
    NovoPedidoSerializer,
    PedidoAdminSerializer,
    PedidoSerializer,
    ServicoAdminSerializer,
    ServicoSerializer,
    UsuarioAdminSerializer,
    UsuarioSerializer,
)

log = logging.getLogger("core.views")


def _arquivo(campo, nome_download=None):
    if not campo:
        raise Http404
    tipo = mimetypes.guess_type(campo.name)[0] or "application/octet-stream"
    nome = nome_download or campo.name.rsplit("/", 1)[-1]
    return FileResponse(campo.open("rb"), content_type=tipo, as_attachment=True, filename=nome)


# --- Conta ------------------------------------------------------------------

@api_view(["GET", "PATCH"])
def eu(request):
    if request.method == "PATCH":
        ser = UsuarioSerializer(request.user, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save()
        return Response(ser.data)
    return Response(UsuarioSerializer(request.user).data)


# --- Serviços e pedidos -----------------------------------------------------

class ServicosView(generics.ListAPIView):
    serializer_class = ServicoSerializer
    pagination_class = None
    queryset = Servico.objects.filter(ativo=True)


class PedidosView(generics.ListAPIView):
    serializer_class = PedidoSerializer

    def get_queryset(self):
        qs = Pedido.objects.filter(usuario=self.request.user).select_related("servico")
        if s := self.request.query_params.get("status"):
            qs = qs.filter(status=s)
        if p := self.request.query_params.get("placa"):
            qs = qs.filter(placa__icontains=p.upper().replace("-", ""))
        return qs

    def get_throttles(self):
        if self.request.method == "POST":
            self.throttle_scope = "pedidos"
            return [ScopedRateThrottle()]
        return super().get_throttles()

    def post(self, request):
        ser = NovoPedidoSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            pedido = servicos.criar_pedido(request.user, ser.validated_data["servico"], ser.validated_data["placa"], dados=ser.validated_data.get("dados"))
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except servicos.SaldoInsuficiente as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_402_PAYMENT_REQUIRED)
        codigo = status.HTTP_201_CREATED if not pedido.aberto else status.HTTP_202_ACCEPTED
        return Response(PedidoSerializer(pedido).data, status=codigo)


def _pedido_do_usuario(request, pk):
    qs = Pedido.objects.select_related("servico")
    if not request.user.is_staff:
        qs = qs.filter(usuario=request.user)
    return get_object_or_404(qs, pk=pk)


@api_view(["GET"])
def pedido_detalhe(request, pk):
    pedido = _pedido_do_usuario(request, pk)
    return Response(PedidoSerializer(pedido).data)


@api_view(["POST"])
def pedido_atualizar(request, pk):
    """Verifica o andamento na hora (limitado a 1x por minuto por pedido)."""
    pedido = servicos.sincronizar_pedido(_pedido_do_usuario(request, pk))
    return Response(PedidoSerializer(pedido).data)


@api_view(["GET"])
def pedido_documento(request, pk):
    pedido = _pedido_do_usuario(request, pk)
    ext = pedido.documento.name.rsplit(".", 1)[-1] if pedido.documento else "pdf"
    return _arquivo(pedido.documento, f"{pedido.servico.codigo.upper()}-{pedido.placa}.{ext}")


# --- Carteira -------------------------------------------------------------------

class ExtratoView(generics.ListAPIView):
    serializer_class = MovimentacaoSerializer

    def get_queryset(self):
        return Movimentacao.objects.filter(usuario=self.request.user)


# --- Administração ----------------------------------------------------------

@api_view(["GET"])
@permission_classes([EhAdmin])
def admin_resumo(request):
    saldo_fonte = None
    try:
        dados = ClienteFonte().saldo()
        saldo_fonte = {
            "modo": dados.get("modoCobranca"),
            "saldo_centavos": dados.get("saldoDisponivelCentavos"),
        }
    except ErroFonte as exc:
        saldo_fonte = {"erro": exc.mensagem_publica}

    pedidos = Pedido.objects.aggregate(
        abertos=Count("id", filter=Q(status__in=Pedido.ABERTOS)),
        concluidos=Count("id", filter=Q(status=Pedido.Status.SUCESSO)),
        receita=Sum("valor_centavos", filter=Q(status=Pedido.Status.SUCESSO)),
        custo=Sum("custo_centavos", filter=Q(status=Pedido.Status.SUCESSO)),
    )
    return Response({
        "saldo_fonte": saldo_fonte,
        "pedidos": pedidos,
        "pagamentos": Cobranca.objects.filter(status=Cobranca.Status.PAGA).aggregate(
            total=Count("id"), recebido=Sum("valor_centavos"),
            hoje=Sum("valor_centavos", filter=Q(pago_em__date=timezone.localdate())),
        ),
        "pagamentos_pendentes": Cobranca.objects.filter(status=Cobranca.Status.PENDENTE).count(),
        "reembolsos_pendentes": Pedido.objects.filter(reembolso_pendente=True).count(),
        "pedidos_site": Pedido.objects.filter(origem=Pedido.Origem.PUBLICO, status=Pedido.Status.SUCESSO).count(),
        "saldo_clientes_centavos": Usuario.objects.aggregate(s=Sum("saldo_centavos"))["s"] or 0,
    })


class AdminUsuariosView(generics.ListCreateAPIView):
    serializer_class = UsuarioAdminSerializer
    permission_classes = [EhAdmin]

    def get_queryset(self):
        qs = Usuario.objects.order_by("username")
        if b := self.request.query_params.get("busca"):
            qs = qs.filter(Q(username__icontains=b) | Q(first_name__icontains=b) | Q(documento__icontains=b))
        return qs


class AdminUsuarioDetalheView(generics.RetrieveUpdateAPIView):
    serializer_class = UsuarioAdminSerializer
    permission_classes = [EhAdmin]
    queryset = Usuario.objects.all()


@api_view(["POST"])
@permission_classes([EhAdmin])
def admin_ajuste_saldo(request, pk):
    try:
        valor = int(request.data.get("valor_centavos"))
    except (TypeError, ValueError):
        return Response({"detail": "Informe valor_centavos."}, status=400)
    descricao = (request.data.get("descricao") or "Ajuste manual").strip()[:255]
    try:
        servicos.ajustar_saldo(pk, valor, f"{descricao} (por {request.user.username})")
    except servicos.SaldoInsuficiente as exc:
        return Response({"detail": str(exc)}, status=400)
    except Usuario.DoesNotExist as exc:
        raise Http404 from exc
    return Response(UsuarioAdminSerializer(Usuario.objects.get(pk=pk)).data)


class AdminServicosView(generics.ListAPIView):
    serializer_class = ServicoAdminSerializer
    permission_classes = [EhAdmin]
    pagination_class = None
    queryset = Servico.objects.all()


class AdminServicoDetalheView(generics.RetrieveUpdateAPIView):
    serializer_class = ServicoAdminSerializer
    permission_classes = [EhAdmin]
    queryset = Servico.objects.all()


class AdminPedidosView(generics.ListAPIView):
    serializer_class = PedidoAdminSerializer
    permission_classes = [EhAdmin]

    def get_queryset(self):
        qs = Pedido.objects.select_related("servico", "usuario")
        if s := self.request.query_params.get("status"):
            qs = qs.filter(status=s)
        if o := self.request.query_params.get("origem"):
            qs = qs.filter(origem=o)
        if self.request.query_params.get("reembolso") == "1":
            qs = qs.filter(reembolso_pendente=True)
        if p := self.request.query_params.get("placa"):
            qs = qs.filter(placa__icontains=p.upper().replace("-", ""))
        return qs


# --- Retorno (webhook) da fonte -------------------------------------------
# URL: /api/retorno/<WEBHOOK_TOKEN_ROTA>/ — cadastre-a no painel do fornecedor.

@csrf_exempt
@require_POST
def retorno_fonte(request, token):
    if token != settings.WEBHOOK_TOKEN_ROTA:
        raise Http404
    corpo = request.body
    ok = assinatura_valida(
        corpo,
        request.headers.get("x-buscars-timestamp", ""),
        request.headers.get("x-buscars-signature", ""),
        settings.FONTE_DADOS["SEGREDO_WEBHOOK"],
    )
    if not ok:
        log.warning("Retorno com assinatura inválida recusado")
        return HttpResponse(status=401)
    try:
        payload = json.loads(corpo or b"{}")
    except ValueError:
        return HttpResponse(status=400)

    protocolo = extrair_protocolo(payload)
    pedido = Pedido.objects.filter(protocolo_fonte=protocolo).select_related("servico").first() if protocolo else None
    if pedido:
        # O aviso só "cutuca": a verdade vem do GET de status.
        servicos.sincronizar_pedido(pedido, forcar=True)
    else:
        log.info("Retorno para protocolo desconhecido: %s", protocolo)
    return HttpResponse(status=204)


@api_view(["POST"])
@permission_classes([EhAdmin])
def admin_pedido_reembolsado(request, pk):
    try:
        pedido = servicos.marcar_reembolsado(pk)
    except Pedido.DoesNotExist as exc:
        raise Http404 from exc
    except ValueError as exc:
        return Response({"detail": str(exc)}, status=400)
    return Response(PedidoAdminSerializer(pedido).data)
