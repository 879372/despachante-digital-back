"""Cliente do Checkout Integrado InfinitePay.

- POST /links          -> cria link de pagamento (preço em centavos, mínimo R$ 1,00)
- POST /payment_check  -> confirma pagamento (handle + order_nsu + transaction_nsu + slug)
O webhook NÃO é assinado: todo aviso precisa ser confirmado no /payment_check.
"""

import logging
from datetime import timedelta

import requests
from django.conf import settings
from django.utils import timezone

log = logging.getLogger("core.pagamentos")

BASE = "https://api.checkout.infinitepay.io"
MINIMO_CENTAVOS = 100


class ErroCheckout(Exception):
    pass


def configurado():
    return bool(settings.INFINITEPAY["HANDLE"] or settings.INFINITEPAY["API_KEY"])


def _cabecalhos():
    h = {"content-type": "application/json", "accept": "application/json"}
    if settings.INFINITEPAY["API_KEY"]:
        h["X-Api-Key"] = settings.INFINITEPAY["API_KEY"]
    return h


def _telefone_e164(tel):
    d = "".join(c for c in (tel or "") if c.isdigit())
    if not d:
        return None
    return f"+{d}" if d.startswith("55") and len(d) >= 12 else f"+55{d}"


def criar_link(cobranca, descricao):
    cfg = settings.INFINITEPAY
    corpo = {
        "items": [{"quantity": 1, "price": cobranca.valor_centavos, "description": descricao[:120]}],
        "order_nsu": cobranca.order_nsu,
        "redirect_url": (
            f"{settings.FRONTEND_URL}/c/retorno?cobranca={cobranca.id}&t={cobranca.token_acesso}"
            if cobranca.tipo == "PUBLICO" else f"{settings.FRONTEND_URL}/pagamento/retorno?cobranca={cobranca.id}"
        ),
    }
    if cfg["HANDLE"]:
        corpo["handle"] = cfg["HANDLE"]
    if cfg["WEBHOOK_BASE"]:
        corpo["webhook_url"] = f"{cfg['WEBHOOK_BASE'].rstrip('/')}/api/pagamentos/retorno/{cfg['WEBHOOK_TOKEN']}/"
    u = cobranca.usuario
    if u:
        cliente = {"name": u.first_name or u.username, "email": u.email or None, "phone_number": _telefone_e164(u.telefone)}
    else:
        cliente = {"name": cobranca.comprador_nome, "email": cobranca.comprador_email or None,
                   "phone_number": _telefone_e164(cobranca.comprador_telefone)}
    cliente = {k: v for k, v in cliente.items() if v}
    if cliente.get("name"):
        corpo["customer"] = cliente
    if cfg["API_KEY"]:  # options exigem API Key
        corpo["options"] = {
            "single_payment": True,
            "valid_until": (timezone.localtime() + timedelta(hours=cfg["VALIDADE_HORAS"])).isoformat(timespec="seconds"),
            "payment_methods_enabled": ["credit_card", "pix"],
            "assured_installments": 0,
        }
    try:
        r = requests.post(f"{BASE}/links", json=corpo, headers=_cabecalhos(), timeout=20)
        dados = r.json() if r.content else {}
    except (requests.RequestException, ValueError) as exc:
        log.error("Checkout: falha ao criar link: %s", exc)
        raise ErroCheckout("Não foi possível iniciar o pagamento agora. Tente novamente.") from exc
    if r.status_code >= 400:
        log.error("Checkout: HTTP %s ao criar link: %s", r.status_code, str(dados)[:300])
        raise ErroCheckout("Não foi possível iniciar o pagamento agora. Tente novamente.")
    url = _achar_url(dados)
    if not url:
        log.error("Checkout: resposta sem URL: %s", str(dados)[:300])
        raise ErroCheckout("Não foi possível iniciar o pagamento agora. Tente novamente.")
    return url, (dados.get("slug") or dados.get("invoice_slug") or "")


def _achar_url(dados):
    if isinstance(dados, str) and dados.startswith("http"):
        return dados
    if not isinstance(dados, dict):
        return ""
    for chave in ("url", "link", "checkout_url", "payment_url", "link_url"):
        v = dados.get(chave)
        if isinstance(v, str) and v.startswith("http"):
            return v
    for v in dados.values():
        if isinstance(v, dict) and (achado := _achar_url(v)):
            return achado
    return ""


def verificar_pagamento(cobranca, transaction_nsu, slug):
    """Consulta oficial. Retorna dict (paid/amount/...) ou None se não confirmou.
    Levanta ErroCheckout se não foi possível consultar (rede, 5xx): quem chamou pode tentar de novo."""
    if not (transaction_nsu and slug):
        return None
    corpo = {"order_nsu": cobranca.order_nsu, "transaction_nsu": transaction_nsu, "slug": slug}
    if settings.INFINITEPAY["HANDLE"]:
        corpo["handle"] = settings.INFINITEPAY["HANDLE"]
    try:
        r = requests.post(f"{BASE}/payment_check", json=corpo, headers=_cabecalhos(), timeout=20)
        dados = r.json()
    except (requests.RequestException, ValueError) as exc:
        log.warning("Checkout: falha no payment_check da cobrança %s: %s", cobranca.pk, exc)
        raise ErroCheckout("Não foi possível confirmar o pagamento agora.") from exc
    if r.status_code >= 500 or r.status_code == 429:
        log.warning("Checkout: payment_check HTTP %s para cobrança %s", r.status_code, cobranca.pk)
        raise ErroCheckout("Não foi possível confirmar o pagamento agora.")
    if r.status_code >= 400 or not dados.get("success"):
        log.info("Checkout: payment_check não confirmou cobrança %s: %s", cobranca.pk, str(dados)[:200])
        return None
    return dados
