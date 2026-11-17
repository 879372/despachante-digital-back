"""Cliente da API de comunicação de venda (SCVV).

Fluxo: login (JWT, 1h) -> inserir (protocolo) -> transmitir -> consultar situação -> comprovante (PDF).
Como o outro cliente, este módulo é o ÚNICO que conhece a fonte: as views só recebem
mensagens públicas e dados higienizados.
"""

import hashlib
import logging
import re
from urllib.parse import quote

import requests
from django.conf import settings
from django.core.cache import cache

log = logging.getLogger("core.integracao")

CHAVE_TOKEN = "carven:token"

# situacao.codigo
CADASTRADA, COMUNICADA, CERTIDAO, CANCELADA, ERRO, EXCLUIDA, BLOQUEADA, NAO_DESBLOQUEADA = 1, 2, 3, 4, 5, 6, 7, 8
SITUACOES = {
    1: "Cadastrada", 2: "Comunicada", 3: "Certidão gerada", 4: "Cancelada",
    5: "Comunicação com erro", 6: "Excluída sem enviar", 7: "Bloqueada", 8: "Não desbloqueada",
}

MENSAGENS = {
    401: "Serviço temporariamente indisponível. Tente novamente mais tarde.",
    403: "Serviço temporariamente indisponível. Tente novamente mais tarde.",
    404: "Comunicação não localizada.",
    422: "Os dados informados foram recusados pelo órgão.",
}
PADRAO = "Não foi possível concluir a comunicação de venda agora."
_PROIBIDO = re.compile(r"carven|scvv", re.I)


class ErroComunicado(Exception):
    def __init__(self, interno, status_http=None, validacao=None, rede=False, nao_enviado=False):
        super().__init__(interno)
        self.interno = interno
        self.status_http = status_http
        self.validacao = [_PROIBIDO.sub("", str(v)).strip() for v in (validacao or []) if str(v).strip()]
        self.rede = rede
        self.nao_enviado = nao_enviado

    @property
    def temporario(self):
        return self.rede or self.status_http in (429, 502, 503, 504) or (self.status_http or 0) >= 500

    @property
    def mensagem_publica(self):
        base = MENSAGENS.get(self.status_http, PADRAO)
        if self.validacao:
            return (f"{base} " + "; ".join(self.validacao[:4]))[:170]
        return base


def configurado():
    cfg = settings.CARVEN
    return bool(cfg["CLIENT_ID"] and cfg["CLIENT_SECRET"])


class ClienteComunicado:
    def __init__(self):
        cfg = settings.CARVEN
        self.url = cfg["URL"].rstrip("/")
        self.client_id = cfg["CLIENT_ID"]
        self.client_secret = cfg["CLIENT_SECRET"]
        self.timeout = cfg["TIMEOUT"]
        identidade = f"{self.url}:{self.client_id}:{self.client_secret}"
        self.chave_token = CHAVE_TOKEN + ":" + hashlib.sha256(identidade.encode()).hexdigest()[:24]

    # --- autenticação -----------------------------------------------------------
    def _token(self, renovar=False):
        if not renovar and (tok := cache.get(self.chave_token)):
            return tok
        if not configurado():
            raise ErroComunicado("Credenciais não configuradas", status_http=401, nao_enviado=True)
        try:
            r = requests.post(f"{self.url}/auth/login", json={"client_id": self.client_id, "client_secret": self.client_secret},
                              headers={"Accept": "application/json", "Content-Type": "application/json"},
                              timeout=self.timeout, allow_redirects=False)
            if 300 <= r.status_code < 400:
                raise ErroComunicado("Login redirecionado; confira URL da API e WAF", status_http=503, nao_enviado=True)
            dados = r.json()
        except (requests.RequestException, ValueError) as exc:
            raise ErroComunicado("Falha de conexão ou resposta inválida no login", rede=True, nao_enviado=True) from exc
        if not isinstance(dados, dict):
            raise ErroComunicado("Resposta de login inválida", status_http=502, nao_enviado=True)
        if r.status_code != 200 or not dados.get("access_token"):
            log.error("Comunicado: login recusado (HTTP %s %s)", r.status_code, dados.get("cod"))
            raise ErroComunicado(f"Login recusado: {dados.get('cod')}",
                                 status_http=r.status_code if r.status_code >= 400 else 502, nao_enviado=True)
        try:
            ttl = max(1, int(dados.get("expires_after") or 3600) - 120)
        except (ValueError, TypeError):
            ttl = 3480
        cache.set(self.chave_token, dados["access_token"], ttl)
        return dados["access_token"]

    def _req(self, metodo, caminho, corpo=None, binario=False, tentou=False):
        cab = {"Authorization": f"Bearer {self._token()}", "Accept": "application/pdf" if binario else "application/json",
               "Cache-Control": "no-cache", "Content-Type": "application/json"}
        try:
            r = requests.request(metodo, f"{self.url}{caminho}", json=corpo, headers=cab,
                                 timeout=self.timeout, allow_redirects=False)
        except requests.RequestException as exc:
            log.warning("Comunicado: erro de rede em %s %s: %s", metodo, caminho, exc)
            raise ErroComunicado(f"Erro de rede: {exc}", rede=True) from exc
        if r.status_code == 401 and not tentou:
            cache.delete(self.chave_token)
            return self._req(metodo, caminho, corpo, binario, tentou=True)
        if 300 <= r.status_code < 400:
            raise ErroComunicado("API redirecionada; confira URL e WAF", status_http=503)
        if binario and r.status_code == 200:
            if not r.content.lstrip().startswith(b"%PDF-"):
                raise ErroComunicado("Comprovante recebido não é PDF", status_http=502)
            return r.content
        try:
            dados = r.json()
        except ValueError:
            dados = {}
        if not isinstance(dados, dict):
            raise ErroComunicado("Resposta inválida da integração", status_http=502)
        if r.status_code >= 400:
            log.warning("Comunicado: HTTP %s em %s %s: %s %s", r.status_code, metodo, caminho, dados.get("cod"), dados.get("msg"))
            raise ErroComunicado(dados.get("msg") or f"HTTP {r.status_code}", status_http=r.status_code,
                                 validacao=dados.get("validacao"))
        if dados.get("status") == "erro" or not dados:
            raise ErroComunicado("Resposta sem confirmação de sucesso", status_http=502)
        return dados

    # --- operações --------------------------------------------------------------
    def inserir(self, payload):
        """Retorna (protocolo, bloqueada)."""
        dados = self._req("POST", "/comunicacao-venda/inserir", payload)
        protocolo = str(dados.get("protocolo") or "")
        if not protocolo:
            raise ErroComunicado(f"Inserção sem protocolo: {dados.get('cod')}", status_http=500)
        return protocolo, dados.get("cod") == "API006" or dados.get("status") == "alerta"

    def transmitir(self, protocolo):
        return self._req("POST", f"/comunicacao-venda/transmitir/enviar/{quote(str(protocolo), safe='')}")

    def detalhes(self, protocolo):
        return self._req("GET", f"/comunicacao-venda/{quote(str(protocolo), safe='')}")

    def comprovante(self, protocolo):
        return self._req("GET", f"/comunicacao-venda/comprovante/{quote(str(protocolo), safe='')}", binario=True)


def situacao(dados):
    dados = dados if isinstance(dados, dict) else {}
    s = dados.get("situacao") or {}
    s = s if isinstance(s, dict) else {}
    try:
        codigo = int(s.get("codigo"))
    except (TypeError, ValueError):
        codigo = 0
    erro = dados.get("ultimo_erro") or s.get("ultimo_erro") or {}
    erro = erro if isinstance(erro, dict) else {}
    descricao = erro.get("descricacao") or erro.get("descricao") or ""
    return codigo, _PROIBIDO.sub("", str(descricao)).strip()[:300]

