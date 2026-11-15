from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Envia um e-mail de teste para conferir a configuração do Resend. Uso: manage.py testar_email voce@email.com"

    def add_arguments(self, parser):
        parser.add_argument("destino")

    def handle(self, destino, **opts):
        backend = settings.EMAIL_BACKEND.rsplit(".", 2)[-2]
        self.stdout.write(f"Backend: {backend} · Remetente: {settings.DEFAULT_FROM_EMAIL}")
        if backend == "console":
            self.stdout.write(self.style.WARNING("EMAIL_SENHA vazio: o e-mail será só impresso aqui, não enviado."))
        try:
            send_mail(
                f"Teste de e-mail · {settings.NOME_SISTEMA}",
                "Se você recebeu esta mensagem, o envio de e-mails está funcionando.",
                settings.DEFAULT_FROM_EMAIL,
                [destino],
            )
        except Exception as exc:  # noqa: BLE001
            raise CommandError(f"Falha no envio: {exc}") from exc
        self.stdout.write(self.style.SUCCESS(f"E-mail enviado para {destino}."))
