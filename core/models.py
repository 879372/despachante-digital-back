import re
import secrets
import unicodedata
import uuid

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models


class Usuario(AbstractUser):
    """Cliente do despachante (ou operador). Tem carteira própria em centavos."""

    telefone = models.CharField(max_length=20, blank=True)
    documento = models.CharField("CPF/CNPJ", max_length=18, blank=True)
    saldo_centavos = models.BigIntegerField(default=0)

    # Programa de indicação
    codigo_indicacao = models.CharField(max_length=16, unique=True, null=True, blank=True)
    indicado_por = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="indicados"
    )
    bonus_indicacao_pago = models.BooleanField(default=False)

    class Meta:
        verbose_name = "usuário"
        constraints = [
            models.UniqueConstraint(
                fields=["documento"], condition=~models.Q(documento=""), name="documento_unico"
            ),
        ]

    def save(self, *args, **kwargs):
        if not self.codigo_indicacao:
            self.codigo_indicacao = gerar_codigo_indicacao(self.first_name or self.username)
        super().save(*args, **kwargs)


def caminho_privado(pasta):
    """Nome aleatório no armazenamento (S3 ou disco): não expõe o nome original nem é adivinhável."""

    def _caminho(instancia, nome_original):
        from django.utils import timezone

        ext = (nome_original.rsplit(".", 1)[-1].lower() if "." in nome_original else "bin")[:5]
        agora = timezone.now()
        return f"{pasta}/{agora:%Y/%m}/{uuid.uuid4().hex}.{ext}"

    _caminho.__name__ = f"caminho_{pasta}"
    _caminho.__qualname__ = f"caminho_{pasta}"
    return _caminho


def caminho_documentos(instancia, nome):
    return caminho_privado("documentos")(instancia, nome)


def caminho_comprovantes(instancia, nome):  # usado pela migração 0007 (histórico)
    return caminho_privado("comprovantes")(instancia, nome)


def caminho_chat(instancia, nome):
    return caminho_privado("chat")(instancia, nome)


def gerar_codigo_indicacao(base=""):
    texto = unicodedata.normalize("NFKD", base or "").encode("ascii", "ignore").decode().upper()
    prefixo = re.sub(r"[^A-Z]", "", texto.split("@")[0].split(" ")[0])[:6] or "INDICA"
    for _ in range(20):
        codigo = f"{prefixo}{secrets.randbelow(9000) + 1000}"
        if not Usuario.objects.filter(codigo_indicacao=codigo).exists():
            return codigo
    return f"{prefixo}{secrets.token_hex(3).upper()}"


class Servico(models.Model):
    codigo = models.SlugField(unique=True, help_text="Código interno usado pelo frontend")
    nome = models.CharField(max_length=80)
    descricao = models.CharField(max_length=255, blank=True)
    preco_centavos = models.PositiveIntegerField(help_text="Preço de venda ao cliente")
    # Dado interno: identificador do produto na fonte. Nunca é serializado.
    sku_fonte = models.CharField(max_length=80)
    assincrono = models.BooleanField(default=False, help_text="Emissão demorada (ex.: CRLV-e)")
    ativo = models.BooleanField(default=True, help_text="Disponível no painel dos despachantes")
    # Site público (cliente final, sem login)
    preco_publico_centavos = models.PositiveIntegerField(default=0, help_text="Preço no site público")
    publico_ativo = models.BooleanField(default=False, help_text="Disponível no site público")
    ordem = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["ordem", "nome"]
        verbose_name = "serviço"

    def __str__(self):
        return self.nome


class Pedido(models.Model):
    class Status(models.TextChoices):
        PENDENTE = "PENDENTE", "Pendente"
        PROCESSANDO = "PROCESSANDO", "Em processamento"
        SUCESSO = "SUCESSO", "Concluído"
        SEM_RESULTADO = "SEM_RESULTADO", "Sem resultado"
        FALHA = "FALHA", "Falhou"

    ABERTOS = (Status.PENDENTE, Status.PROCESSANDO)

    class Origem(models.TextChoices):
        PAINEL = "PAINEL", "Painel"
        PUBLICO = "PUBLICO", "Site público"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="pedidos")
    origem = models.CharField(max_length=8, choices=Origem.choices, default=Origem.PAINEL, db_index=True)
    # Comprador do site público (sem conta). O token dá acesso ao resultado.
    comprador_nome = models.CharField(max_length=150, blank=True)
    comprador_email = models.EmailField(blank=True, db_index=True)
    comprador_telefone = models.CharField(max_length=20, blank=True)
    token_acesso = models.CharField(max_length=64, unique=True, null=True, blank=True)
    reembolso_pendente = models.BooleanField(default=False)
    reembolsado_em = models.DateTimeField(null=True, blank=True)
    servico = models.ForeignKey(Servico, on_delete=models.PROTECT)
    placa = models.CharField(max_length=7, db_index=True)
    status = models.CharField(max_length=15, choices=Status.choices, default=Status.PENDENTE, db_index=True)
    valor_centavos = models.PositiveIntegerField()
    mensagem = models.CharField(max_length=255, blank=True)

    # Resultado já higienizado (sem referência à fonte)
    resultado = models.JSONField(null=True, blank=True)
    destaque = models.CharField(max_length=120, blank=True, help_text="Ex.: código de segurança")
    documento = models.FileField(upload_to=caminho_documentos, blank=True, max_length=200)

    # Controle interno da integração (nunca serializado)
    chave_idempotencia = models.CharField(max_length=64, unique=True)
    protocolo_fonte = models.CharField(max_length=80, blank=True, db_index=True)
    enviado = models.BooleanField(default=False, help_text="A fonte aceitou a solicitação")
    tentativas_envio = models.PositiveSmallIntegerField(default=0)
    custo_centavos = models.IntegerField(null=True, blank=True)
    cobrado_fonte = models.BooleanField(null=True)
    estornado = models.BooleanField(default=False)
    ultima_verificacao = models.DateTimeField(null=True, blank=True)

    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-criado_em"]

    def __str__(self):
        return f"{self.servico} · {self.placa} · {self.status}"

    @property
    def aberto(self):
        return self.status in self.ABERTOS


class Movimentacao(models.Model):
    class Tipo(models.TextChoices):
        RECARGA = "RECARGA", "Recarga"
        DEBITO = "DEBITO", "Pedido"
        ESTORNO = "ESTORNO", "Estorno"
        AJUSTE = "AJUSTE", "Ajuste manual"
        BONUS = "BONUS", "Bônus de indicação"
        PAGAMENTO = "PAGAMENTO", "Pagamento online"

    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="movimentacoes")
    tipo = models.CharField(max_length=10, choices=Tipo.choices)
    valor_centavos = models.BigIntegerField(help_text="Positivo = entrada, negativo = saída")
    saldo_apos_centavos = models.BigIntegerField()
    descricao = models.CharField(max_length=255)
    pedido = models.ForeignKey(Pedido, null=True, blank=True, on_delete=models.SET_NULL)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-criado_em", "-id"]
        verbose_name = "movimentação"
        verbose_name_plural = "movimentações"


class Conversa(models.Model):
    class Status(models.TextChoices):
        ABERTA = "ABERTA", "Aberta"
        RESOLVIDA = "RESOLVIDA", "Resolvida"

    cliente = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE, related_name="conversas")
    # Visitante do site público (sem conta)
    visitante_nome = models.CharField(max_length=120, blank=True)
    visitante_contato = models.CharField(max_length=150, blank=True)
    visitante_token = models.CharField(max_length=64, unique=True, null=True, blank=True)
    assunto = models.CharField(max_length=120)
    pedido = models.ForeignKey(Pedido, null=True, blank=True, on_delete=models.SET_NULL, related_name="conversas")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ABERTA, db_index=True)
    ultima_mensagem_em = models.DateTimeField(auto_now_add=True, db_index=True)
    lido_cliente_em = models.DateTimeField(null=True, blank=True)
    lido_equipe_em = models.DateTimeField(null=True, blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-ultima_mensagem_em"]

    def __str__(self):
        return f"#{self.pk} {self.assunto}"


class Mensagem(models.Model):
    conversa = models.ForeignKey(Conversa, on_delete=models.CASCADE, related_name="mensagens")
    autor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    da_equipe = models.BooleanField(default=False)
    texto = models.TextField(max_length=2000, blank=True)
    anexo = models.FileField(upload_to=caminho_chat, blank=True, max_length=200)
    anexo_nome = models.CharField(max_length=120, blank=True)
    criado_em = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["criado_em", "id"]


class Cobranca(models.Model):
    """Pagamento online (checkout). O id vira o order_nsu no provedor de pagamento."""

    class Tipo(models.TextChoices):
        RECARGA = "RECARGA", "Recarga de saldo"
        PEDIDO = "PEDIDO", "Pagamento de consulta"
        PUBLICO = "PUBLICO", "Consulta no site público"

    class Status(models.TextChoices):
        PENDENTE = "PENDENTE", "Aguardando pagamento"
        PAGA = "PAGA", "Paga"
        CANCELADA = "CANCELADA", "Cancelada"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="cobrancas")
    tipo = models.CharField(max_length=10, choices=Tipo.choices)
    # Comprador do site público
    comprador_nome = models.CharField(max_length=150, blank=True)
    comprador_email = models.EmailField(blank=True)
    comprador_telefone = models.CharField(max_length=20, blank=True)
    token_acesso = models.CharField(max_length=64, blank=True, help_text="Vira o token do pedido público")
    valor_centavos = models.PositiveIntegerField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDENTE, db_index=True)
    # Para pagamento de consulta: o que emitir depois de pago
    servico = models.ForeignKey(Servico, null=True, blank=True, on_delete=models.PROTECT)
    placa = models.CharField(max_length=7, blank=True)
    pedido = models.ForeignKey(Pedido, null=True, blank=True, on_delete=models.SET_NULL, related_name="cobrancas")
    mensagem = models.CharField(max_length=255, blank=True)
    # Dados do checkout
    link_url = models.URLField(max_length=500, blank=True)
    slug = models.CharField(max_length=80, blank=True, db_index=True)
    transaction_nsu = models.CharField(max_length=80, blank=True)
    capture_method = models.CharField(max_length=20, blank=True)
    valor_pago_centavos = models.PositiveIntegerField(null=True, blank=True)
    recibo_url = models.URLField(max_length=500, blank=True)
    pago_em = models.DateTimeField(null=True, blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-criado_em"]
        verbose_name = "cobrança"

    @property
    def order_nsu(self):
        return self.id.hex
