from django.core.management.base import BaseCommand

from core.servicos import sincronizar_abertos, sincronizar_cobrancas


class Command(BaseCommand):
    help = "Verifica pedidos em aberto (contingência do webhook). Rode via cron a cada 10-15 min."

    def handle(self, *args, **opts):
        total = sincronizar_abertos()
        cobrancas = sincronizar_cobrancas()
        self.stdout.write(self.style.SUCCESS(f"{total} pedido(s) e {cobrancas} cobrança(s) verificados."))
