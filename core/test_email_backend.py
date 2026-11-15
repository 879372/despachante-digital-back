from unittest.mock import Mock, patch

import requests
from django.core.mail import EmailMultiAlternatives
from django.test import SimpleTestCase, override_settings

from .email_backend import ResendBackend


@override_settings(EMAIL_HOST_PASSWORD="re_teste", EMAIL_TIMEOUT=15)
class ResendBackendTeste(SimpleTestCase):
    @patch("core.email_backend.requests.post")
    def test_envia_texto_html_e_reply_to_por_https(self, post):
        post.return_value = Mock(ok=True, json=lambda: {"id": "email-123"})
        msg = EmailMultiAlternatives("Redefinir senha", "Texto", "app@teste.com", ["cliente@teste.com"],
                                     reply_to=["suporte@teste.com"])
        msg.attach_alternative("<p>Redefinir senha</p>", "text/html")
        self.assertEqual(ResendBackend().send_messages([msg]), 1)
        self.assertEqual(post.call_args.args, ("https://api.resend.com/emails",))
        dados = post.call_args.kwargs
        self.assertEqual(dados["json"]["html"], "<p>Redefinir senha</p>")
        self.assertEqual(dados["json"]["reply_to"], ["suporte@teste.com"])
        self.assertEqual(dados["headers"]["Authorization"], "Bearer re_teste")

    @patch("core.email_backend.requests.post")
    def test_rejeicao_nao_conta_como_enviado(self, post):
        post.return_value = Mock(ok=False, status_code=403)
        msg = EmailMultiAlternatives("Teste", "Texto", "app@teste.com", ["cliente@teste.com"])
        with self.assertRaises(RuntimeError):
            ResendBackend().send_messages([msg])
        self.assertEqual(ResendBackend(fail_silently=True).send_messages([msg]), 0)

    @patch("core.email_backend.requests.post", side_effect=requests.Timeout)
    def test_timeout_nao_conta_como_enviado(self, post):
        msg = EmailMultiAlternatives("Teste", "Texto", "app@teste.com", ["cliente@teste.com"])
        with self.assertRaises(RuntimeError):
            ResendBackend().send_messages([msg])
