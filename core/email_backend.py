import logging

import requests
from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend

log = logging.getLogger("core.emails")


class ResendBackend(BaseEmailBackend):
    """Envia os e-mails transacionais pela API HTTPS do Resend."""

    def send_messages(self, email_messages):
        enviados = 0
        for mensagem in email_messages or []:
            if not mensagem.recipients():
                continue
            corpo = {"from": mensagem.from_email, "to": mensagem.to,
                     "subject": mensagem.subject, "text": mensagem.body}
            for campo in ("cc", "bcc", "reply_to"):
                if valor := getattr(mensagem, campo, None):
                    corpo[campo] = valor
            if mensagem.content_subtype == "html":
                corpo.pop("text")
                corpo["html"] = mensagem.body
            for conteudo, tipo in getattr(mensagem, "alternatives", []):
                if tipo == "text/html":
                    corpo["html"] = conteudo
            try:
                resposta = requests.post(
                    "https://api.resend.com/emails", json=corpo,
                    headers={"Authorization": f"Bearer {settings.EMAIL_HOST_PASSWORD}",
                             "Content-Type": "application/json"},
                    timeout=settings.EMAIL_TIMEOUT,
                )
                if not resposta.ok:
                    # Não registrar o corpo, links privados nem a chave de API.
                    raise RuntimeError(f"Resend recusou o envio (HTTP {resposta.status_code}).")
                identificador = resposta.json().get("id")
                if not identificador:
                    raise RuntimeError("Resend não retornou o identificador do envio.")
                log.info("Resend aceitou o e-mail: %s", identificador)
                enviados += 1
            except (requests.RequestException, ValueError, RuntimeError) as exc:
                if not self.fail_silently:
                    raise RuntimeError("Falha ao enviar e-mail pela API do Resend.") from exc
        return enviados
