from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.integracao.carven import ClienteComunicado, ErroComunicado
from core.models import Pedido, Servico


class Command(BaseCommand):
    help = "Vincula protocolo conferido na Carven a um comunicado cujo envio ficou sem confirmação. Não transmite."

    def add_arguments(self, parser):
        parser.add_argument("pedido", help="UUID do pedido")
        parser.add_argument("protocolo", help="Protocolo localizado e conferido no painel da Carven")

    def handle(self, *args, **opts):
        protocolo = opts["protocolo"].strip()
        if not protocolo or len(protocolo) > 80:
            raise CommandError("Protocolo inválido.")
        try:
            pedido = Pedido.objects.select_related("servico").get(pk=opts["pedido"], servico__fonte=Servico.Fonte.COMUNICADO)
        except (Pedido.DoesNotExist, ValueError, ValidationError):
            raise CommandError("Pedido de comunicado não encontrado.")
        if not pedido.aberto or not pedido.envio_incerto or pedido.protocolo_fonte:
            raise CommandError("O pedido não está aguardando reconciliação de inserção.")
        try:
            dados = ClienteComunicado().detalhes(protocolo)
        except ErroComunicado:
            raise CommandError("Não foi possível validar o protocolo na fonte.")
        from core.validacoes import so_digitos
        veiculo = dados.get("veiculo") or {}
        vendedor = dados.get("vendedor") or {}
        comprador = dados.get("comprador") or {}
        esperado = pedido.dados_entrada or {}
        confere = (
            str(veiculo.get("placa", "")).replace("-", "").upper() == pedido.placa
            and so_digitos(str(veiculo.get("renavam", ""))).zfill(11) == esperado.get("renavam")
            and so_digitos(str(vendedor.get("cpf") or vendedor.get("cnpj") or "")) == esperado.get("vendedor", {}).get("documento")
            and so_digitos(str(comprador.get("cpf") or comprador.get("cnpj") or "")) == esperado.get("comprador", {}).get("documento")
        )
        if not confere:
            raise CommandError("Veículo, comprador ou vendedor do protocolo não correspondem ao pedido.")
        with transaction.atomic():
            p = Pedido.objects.select_for_update().get(pk=pedido.pk)
            if not p.aberto or not p.envio_incerto or p.protocolo_fonte:
                raise CommandError("O pedido foi alterado. Confira novamente.")
            if Pedido.objects.filter(servico__fonte=Servico.Fonte.COMUNICADO, protocolo_fonte=protocolo).exclude(pk=p.pk).exists():
                raise CommandError("Protocolo já vinculado a outro pedido.")
            p.protocolo_fonte = protocolo
            p.envio_incerto = False
            p.ultima_verificacao = None
            p.mensagem = "Registro conferido. Aguardando atualização do andamento."
            p.save(update_fields=["protocolo_fonte", "envio_incerto", "ultima_verificacao", "mensagem"])
        self.stdout.write(self.style.SUCCESS("Protocolo vinculado. A próxima sincronização consultará a situação antes de transmitir."))
