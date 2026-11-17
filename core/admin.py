from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from . import servicos
from .models import Cobranca, Conversa, Mensagem, Movimentacao, Pedido, Servico, Usuario


@admin.register(Usuario)
class UsuarioAdmin(UserAdmin):
    list_display = ["username", "first_name", "documento", "saldo_centavos", "is_staff", "is_active"]
    readonly_fields = ["saldo_centavos"]
    fieldsets = UserAdmin.fieldsets + (("Despachante", {"fields": ("telefone", "documento", "saldo_centavos")}),)


@admin.register(Servico)
class ServicoAdmin(admin.ModelAdmin):
    list_display = ["nome", "codigo", "preco_centavos", "assincrono", "ativo", "ordem"]
    list_editable = ["preco_centavos", "ativo", "ordem"]


@admin.register(Pedido)
class PedidoAdmin(admin.ModelAdmin):
    list_display = ["criado_em", "usuario", "servico", "placa", "status", "valor_centavos", "custo_centavos", "estornado"]
    list_filter = ["status", "servico", "envio_incerto"]
    search_fields = ["placa", "usuario__username", "protocolo_fonte"]
    readonly_fields = [f.name for f in Pedido._meta.fields]
    actions = ["sincronizar"]

    @admin.action(description="Verificar andamento agora")
    def sincronizar(self, request, queryset):
        for pedido in queryset.select_related("servico"):
            servicos.sincronizar_pedido(pedido, forcar=True)
        self.message_user(request, "Pedidos verificados.")


@admin.register(Movimentacao)
class MovimentacaoAdmin(admin.ModelAdmin):
    list_display = ["criado_em", "usuario", "tipo", "valor_centavos", "saldo_apos_centavos", "descricao"]
    list_filter = ["tipo"]
    search_fields = ["usuario__username", "descricao"]
    readonly_fields = [f.name for f in Movimentacao._meta.fields]

    def has_add_permission(self, request):
        return False


class MensagemInline(admin.TabularInline):
    model = Mensagem
    extra = 0
    readonly_fields = ["autor", "da_equipe", "texto", "anexo", "criado_em"]


@admin.register(Conversa)
class ConversaAdmin(admin.ModelAdmin):
    list_display = ["id", "cliente", "assunto", "status", "ultima_mensagem_em"]
    list_filter = ["status"]
    search_fields = ["assunto", "cliente__username", "cliente__first_name"]
    inlines = [MensagemInline]


@admin.register(Cobranca)
class CobrancaAdmin(admin.ModelAdmin):
    list_display = ["criado_em", "usuario", "tipo", "valor_centavos", "status", "capture_method", "pedido"]
    list_filter = ["status", "tipo", "capture_method"]
    search_fields = ["usuario__username", "placa", "slug", "transaction_nsu"]
    readonly_fields = [f.name for f in Cobranca._meta.fields]
    actions = ["reconferir"]

    @admin.action(description="Reconferir pagamento na InfinitePay")
    def reconferir(self, request, queryset):
        for c in queryset:
            servicos.confirmar_cobranca(c.pk)
        self.message_user(request, "Cobranças reconferidas.")
