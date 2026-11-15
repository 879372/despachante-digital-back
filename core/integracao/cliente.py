"""Cliente da fonte de dados veiculares.

Este é o ÚNICO módulo que conhece a fonte. Nada daqui (URL, chave, nomes de
produto, mensagens cruas) deve chegar ao frontend: as views só expõem o que
passou por `higienizar()` e as mensagens públicas de `ErroFonte`.
"""

import hashlib
import hmac
import logging
import re
import time
from datetime import datetime
from urllib.parse import quote

import requests
from django.conf import settings

log = logging.getLogger("core.integracao")

MENSAGENS_PUBLICAS = {
    400: "Dados inválidos para esta consulta. Confira a placa.",
    401: "Serviço temporariamente indisponível. Tente novamente mais tarde.",
    402: "Serviço temporariamente indisponível. Tente novamente mais tarde.",
    404: "Serviço indisponível no momento.",
    422: "Placa inválida ou não localizada.",
    429: "Muitas solicitações agora. Aguarde alguns instantes.",
    501: "Serviço indisponível no momento.",
    503: "Serviço temporariamente indisponível. Tente novamente mais tarde.",
}
MENSAGEM_PADRAO = "Não foi possível concluir a solicitação agora."


class ErroFonte(Exception):
    def __init__(self, interno, status_http=None, detalhes=None, rede=False):
        super().__init__(interno)
        self.interno = interno
        self.status_http = status_http
        self.detalhes = detalhes or {}
        self.rede = rede

    @property
    def temporario(self):
        """Pode ser reenviado depois com a mesma chave de idempotência."""
        return self.rede or self.status_http in (429, 503) or (self.status_http or 0) >= 500

    @property
    def mensagem_publica(self):
        return MENSAGENS_PUBLICAS.get(self.status_http, MENSAGEM_PADRAO)

    @property
    def cobrado(self):
        return bool(self.detalhes.get("cobrado")) if isinstance(self.detalhes, dict) else False


class ClienteFonte:
    def __init__(self, url=None, chave=None, timeout=None):
        cfg = settings.FONTE_DADOS
        self.url = (url or cfg["URL"]).rstrip("/")
        self.chave = chave or cfg["CHAVE"]
        self.timeout = timeout or cfg["TIMEOUT"]

    def _requisicao(self, metodo, caminho, corpo=None, idempotencia=None, tentativas=3):
        if not self.chave:
            raise ErroFonte("Chave da fonte não configurada", status_http=401)
        cabecalhos = {
            "x-api-key": self.chave,
            "accept": "application/json",
            "content-type": "application/json",
        }
        if idempotencia:
            cabecalhos["idempotency-key"] = idempotencia

        for tentativa in range(tentativas):
            ultima = tentativa == tentativas - 1
            try:
                resp = requests.request(
                    metodo, f"{self.url}{caminho}", json=corpo, headers=cabecalhos, timeout=self.timeout
                )
            except requests.RequestException as exc:
                log.warning("Fonte: erro de rede em %s %s: %s", metodo, caminho, exc)
                if ultima:
                    raise ErroFonte(f"Erro de rede: {exc}", rede=True) from exc
                time.sleep(2**tentativa)
                continue

            if resp.status_code in (429, 503) and not ultima:
                espera = resp.headers.get("Retry-After")
                espera = int(espera) if espera and espera.isdigit() else 2**tentativa
                time.sleep(min(espera, 10))
                continue

            try:
                dados = resp.json()
            except ValueError:
                dados = {}

            if resp.status_code >= 400 or dados.get("success") is False:
                msg = dados.get("message") or dados.get("mensagem") or dados.get("error") or resp.text[:200]
                nivel = log.error if resp.status_code in (401, 402) else log.warning
                nivel("Fonte: HTTP %s em %s %s: %s", resp.status_code, metodo, caminho, msg)
                raise ErroFonte(msg, status_http=resp.status_code, detalhes=dados.get("details"))

            return resp.status_code, dados.get("data", dados)

        raise ErroFonte("Tentativas esgotadas", rede=True)  # pragma: no cover

    # --- Operações -------------------------------------------------------

    def consultar(self, sku, placa, idempotencia):
        """Consulta síncrona (ex.: código de segurança do CRV)."""
        return self._requisicao("POST", "/v1/consulta", {"sku": sku, "placa": placa}, idempotencia)

    def solicitar_emissao(self, sku, placa, idempotencia):
        """Consulta assíncrona (ex.: CRLV-e). Retorna 202 + consultaId."""
        corpo = {"tipo": "PLACA", "sku": sku, "valor": placa}
        return self._requisicao("POST", "/v1/consultas", corpo, idempotencia)

    def status(self, protocolo):
        return self._requisicao("GET", f"/v1/consultas/{quote(protocolo, safe='')}/status")

    def saldo(self):
        return self._requisicao("GET", "/v1/saldo", tentativas=1)[1]

    def catalogo(self):
        return self._requisicao("GET", "/v1/catalogo", tentativas=1)[1]


# --- Webhook ---------------------------------------------------------------

def assinatura_valida(corpo_cru: bytes, timestamp: str, assinaturas: str, segredo: str, janela=300) -> bool:
    """Confere HMAC-SHA256 de `timestamp.corpo` (cabeçalho pode trazer várias)."""
    if not (segredo and timestamp and assinaturas):
        return False
    try:
        if timestamp.isdigit():
            ts = int(timestamp)
            ts = ts / 1000 if ts > 1e11 else ts
        else:
            ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return False
    if abs(time.time() - ts) > janela:
        return False
    mensagem = timestamp.encode() + b"." + corpo_cru
    esperado = hmac.new(segredo.encode(), mensagem, hashlib.sha256).hexdigest()
    for parte in assinaturas.split(","):
        valor = parte.strip()
        if valor.startswith("sha256="):
            valor = valor[7:]
        if hmac.compare_digest(valor, esperado):
            return True
    return False


def extrair_protocolo(payload: dict) -> str:
    """Pega o id da consulta do aviso. Não seguimos URLs vindas do aviso."""
    dados = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    for chave in ("consultaId", "id", "consulta_id"):
        if dados.get(chave):
            return str(dados[chave])
    url = dados.get("statusUrl") or payload.get("statusUrl") or ""
    m = re.search(r"/consultas/([^/?#]+)/status", url)
    return m.group(1) if m else ""


# --- Higienização ------------------------------------------------------------

_CHAVES_PROIBIDAS = re.compile(
    r"provedor|provider|fonte|source|origem|fornecedor|vendor|sku|fallback|buscars|cobrad|custo|reserva",
    re.I,
)
_TEXTO_PROIBIDO = re.compile(r"buscars", re.I)


def higienizar(valor):
    """Remove qualquer pista da fonte antes de salvar/exibir."""
    if isinstance(valor, dict):
        return {k: higienizar(v) for k, v in valor.items() if not _CHAVES_PROIBIDAS.search(str(k))}
    if isinstance(valor, list):
        return [higienizar(v) for v in valor]
    if isinstance(valor, str):
        return _TEXTO_PROIBIDO.sub("", valor)
    return valor
