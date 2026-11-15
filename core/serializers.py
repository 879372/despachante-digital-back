from rest_framework import serializers

from .models import Movimentacao, Pedido, Servico, Usuario


class UsuarioSerializer(serializers.ModelSerializer):
    class Meta:
        model = Usuario
        fields = ["id", "username", "first_name", "last_name", "email", "telefone", "documento",
                  "saldo_centavos", "is_staff", "codigo_indicacao"]
        read_only_fields = ["saldo_centavos", "is_staff", "codigo_indicacao", "documento", "username"]


class UsuarioAdminSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, required=False, min_length=6)

    class Meta:
        model = Usuario
        fields = ["id", "username", "first_name", "email", "telefone", "documento", "saldo_centavos",
                  "is_staff", "is_active", "password", "date_joined", "codigo_indicacao", "indicado_por_nome"]
        read_only_fields = ["saldo_centavos", "date_joined", "codigo_indicacao"]

    indicado_por_nome = serializers.CharField(source="indicado_por.first_name", read_only=True, default=None)

    def validate_documento(self, v):
        from .validacoes import documento_valido, so_digitos
        d = so_digitos(v)
        if not d:
            return ""
        if not documento_valido(d):
            raise serializers.ValidationError("CPF ou CNPJ inválido.")
        if Usuario.objects.filter(documento=d).exclude(pk=getattr(self.instance, "pk", None)).exists():
            raise serializers.ValidationError("Este CPF/CNPJ já possui cadastro.")
        return d

    def validate_telefone(self, v):
        from .validacoes import so_digitos, telefone_valido
        if v and not telefone_valido(v):
            raise serializers.ValidationError("Telefone inválido.")
        return so_digitos(v)

    def validate_email(self, v):
        v = (v or "").strip().lower()
        if v and Usuario.objects.filter(email__iexact=v).exclude(pk=getattr(self.instance, "pk", None)).exists():
            raise serializers.ValidationError("E-mail já cadastrado.")
        return v

    def create(self, dados):
        senha = dados.pop("password", None)
        usuario = Usuario(**dados)
        if senha:
            usuario.set_password(senha)
        else:
            usuario.set_unusable_password()
        usuario.save()
        return usuario

    def update(self, usuario, dados):
        senha = dados.pop("password", None)
        for campo, valor in dados.items():
            setattr(usuario, campo, valor)
        if senha:
            usuario.set_password(senha)
        usuario.save()
        return usuario


class ServicoSerializer(serializers.ModelSerializer):
    """Público: sem nenhum dado interno da integração."""

    class Meta:
        model = Servico
        fields = ["id", "codigo", "nome", "descricao", "preco_centavos", "assincrono"]


class ServicoAdminSerializer(serializers.ModelSerializer):
    class Meta:
        model = Servico
        fields = ["id", "codigo", "nome", "descricao", "preco_centavos", "assincrono", "ativo", "ordem",
                  "preco_publico_centavos", "publico_ativo"]
        read_only_fields = ["codigo", "assincrono"]

    def validate(self, d):
        ativo = d.get("publico_ativo", getattr(self.instance, "publico_ativo", False))
        preco = d.get("preco_publico_centavos", getattr(self.instance, "preco_publico_centavos", 0))
        if ativo and preco < 100:
            raise serializers.ValidationError({"preco_publico_centavos": "Para ativar no site público, o preço mínimo é R$ 1,00."})
        return d


class PedidoSerializer(serializers.ModelSerializer):
    servico = serializers.CharField(source="servico.nome", read_only=True)
    servico_codigo = serializers.CharField(source="servico.codigo", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    tem_documento = serializers.SerializerMethodField()
    codigo_seguranca = serializers.CharField(source="destaque", read_only=True)

    class Meta:
        model = Pedido
        fields = ["id", "servico", "servico_codigo", "placa", "status", "status_display", "valor_centavos",
                  "mensagem", "codigo_seguranca", "resultado", "tem_documento", "estornado",
                  "criado_em", "atualizado_em"]

    def get_tem_documento(self, obj):
        return bool(obj.documento)


class PedidoAdminSerializer(PedidoSerializer):
    usuario = serializers.SerializerMethodField()

    class Meta(PedidoSerializer.Meta):
        fields = PedidoSerializer.Meta.fields + ["usuario", "custo_centavos", "origem", "comprador_email",
                                                 "comprador_telefone", "reembolso_pendente", "reembolsado_em"]

    def get_usuario(self, obj):
        if obj.usuario_id:
            return obj.usuario.first_name or obj.usuario.username
        return f"{obj.comprador_nome or 'Visitante'} (site)"


class NovoPedidoSerializer(serializers.Serializer):
    servico = serializers.SlugRelatedField(slug_field="codigo", queryset=Servico.objects.filter(ativo=True))
    placa = serializers.CharField(max_length=10)


class MovimentacaoSerializer(serializers.ModelSerializer):
    tipo_display = serializers.CharField(source="get_tipo_display", read_only=True)

    class Meta:
        model = Movimentacao
        fields = ["id", "tipo", "tipo_display", "valor_centavos", "saldo_apos_centavos", "descricao", "criado_em"]
