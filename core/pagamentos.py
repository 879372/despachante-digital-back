"""Endpoints de pagamento online (Checkout Integrado InfinitePay)."""

import json
import logging

from django.conf import settings
from django.db.models import Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from rest_framework import serializers, status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle

from . import servicos
from .integracao import infinitepay
from .models import Cobranca, Servico
from .serializers import NovoPedidoSerializer
from .permissoes import EhAdmin

log = logging.getLogger("core.pagamentos")


class ThrottlePagamento(UserRateThrottle):
    scope = "pagamentos"


def _json(c):
    return {
        "id": str(c.id),
        "tipo": c.tipo,
        "status": c.status,
        "status_display": c.get_status_display(),
        "valor_centavos": c.valor_centavos,
        "url": c.link_url if c.status == Cobranca.Status.PENDENTE else None,
        "pedido_id": str(c.pedido_id) if c.pedido_id else None,
        "servico": c.servico.nome if c.servico_id else None,
        "placa": c.placa,
        "mensagem": c.mensagem,
        "forma": c.capture_method,
        "criado_em": c.criado_em,
        "pago_em": c.pago_em,
    }


def _indisponivel():
    return Response({"detail": "Pagamento online indisponível no momento. Tente novamente mais tarde."},
                    status=status.HTTP_503_SERVICE_UNAVAILABLE)



class PagarPedidoSerializer(NovoPedidoSerializer):
    pass


@api_view(["POST"])
@throttle_classes([ThrottlePagamento])
def pagar_pedido(request):
    if not infinitepay.configurado():
        return _indisponivel()
    ser = PagarPedidoSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    try:
        c = servicos.iniciar_checkout_pedido(request.user, ser.validated_data["servico"], ser.validated_data["placa"], dados=ser.validated_data.get("dados"))
    except ValueError as exc:
        return Response({"detail": str(exc)}, status=400)
    except infinitepay.ErroCheckout as exc:
        return Response({"detail": str(exc)}, status=502)
    return Response(_json(c), status=201)


@api_view(["POST"])
@throttle_classes([ThrottlePagamento])
def pagar_recarga(request):
    if not infinitepay.configurado():
        return _indisponivel()
    try:
        valor = int(request.data.get("valor_centavos"))
    except (TypeError, ValueError):
        return Response({"valor_centavos": ["Informe o valor."]}, status=400)
    try:
        c = servicos.iniciar_checkout_recarga(request.user, valor)
    except ValueError as exc:
        return Response({"valor_centavos": [str(exc)]}, status=400)
    except infinitepay.ErroCheckout as exc:
        return Response({"detail": str(exc)}, status=502)
    return Response(_json(c), status=201)


@api_view(["GET", "POST"])
def cobranca(request, pk):
    """GET: situação. POST: confirmar com os dados que voltam do checkout (slug, transaction_nsu)."""
    c = get_object_or_404(Cobranca, pk=pk, usuario=request.user)
    if request.method == "POST":
        c = servicos.confirmar_cobranca(
            c.pk,
            transaction_nsu=str(request.data.get("transaction_nsu", ""))[:80],
            slug=str(request.data.get("slug", ""))[:80],
        )
    return Response(_json(c))


@api_view(["GET"])
def minhas_cobrancas(request):
    qs = Cobranca.objects.filter(usuario=request.user).select_related("servico")[:30]
    return Response([_json(c) for c in qs])


@csrf_exempt
@require_POST
def webhook(request, token):
    """Aviso do checkout. Sem assinatura: só serve de gatilho; a confirmação é no /payment_check."""
    if token != settings.INFINITEPAY["WEBHOOK_TOKEN"]:
        raise Http404
    try:
        dados = json.loads(request.body or b"{}")
    except ValueError:
        return HttpResponse(status=400)
    order_nsu = str(dados.get("order_nsu") or "")
    c = None
    if len(order_nsu) == 32:
        c = Cobranca.objects.filter(id=order_nsu).first()
    if not c:
        log.info("Webhook de pagamento para order_nsu desconhecido: %s", order_nsu[:40])
        return HttpResponse(status=200)  # não é nosso: não pedir reenvio
    try:
        c = servicos.confirmar_cobranca(
            c.pk, transaction_nsu=str(dados.get("transaction_nsu") or "")[:80],
            slug=str(dados.get("invoice_slug") or dados.get("slug") or "")[:80], levantar_erro=True,
        )
    except infinitepay.ErroCheckout:
        # Não conseguimos confirmar agora: 400 faz a InfinitePay reenviar o aviso mais tarde.
        return HttpResponse(status=400)
    log.info("Webhook de pagamento: cobrança %s -> %s", c.pk, c.status)
    return HttpResponse(status=200)


@api_view(["GET"])
@permission_classes([EhAdmin])
def admin_pagamentos(request):
    qs = Cobranca.objects.select_related("usuario", "servico")
    if s := request.query_params.get("status"):
        qs = qs.filter(status=s)
    if b := request.query_params.get("busca", "").strip():
        qs = qs.filter(
            Q(usuario__first_name__icontains=b) | Q(usuario__username__icontains=b)
            | Q(comprador_nome__icontains=b) | Q(comprador_email__icontains=b)
            | Q(placa__icontains=b.upper())
        )
    lista = []
    for c in qs.order_by("-criado_em")[:100]:
        d = _json(c)
        d["usuario"] = (c.usuario.first_name or c.usuario.username) if c.usuario_id else f"{c.comprador_nome or 'Visitante'} (site)"
        d["valor_pago_centavos"] = c.valor_pago_centavos
        lista.append(d)
    return Response(lista)
