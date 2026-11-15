"""Cadastro público, indicação e recuperação de senha."""

import logging

from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from rest_framework import serializers, status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle
from rest_framework_simplejwt.tokens import RefreshToken

from . import servicos
from .emails import enviar_redefinicao_senha
from .integracao import infinitepay
from .models import Movimentacao, Usuario
from .validacoes import documento_valido, so_digitos, telefone_valido

log = logging.getLogger("core.conta")


class ThrottlePublico(SimpleRateThrottle):
    """Limita rotas públicas (cadastro, senha) por IP."""

    scope = "publico"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


def _buscar_padrinho(codigo):
    codigo = (codigo or "").strip().upper()
    if not codigo:
        return None
    return Usuario.objects.filter(codigo_indicacao__iexact=codigo, is_active=True).first()


class CadastroSerializer(serializers.Serializer):
    nome = serializers.CharField(max_length=150, min_length=3)
    email = serializers.EmailField(max_length=254)
    documento = serializers.CharField(max_length=18)
    telefone = serializers.CharField(max_length=20)
    password = serializers.CharField(write_only=True, min_length=8, max_length=128)
    codigo_indicacao = serializers.CharField(max_length=16, required=False, allow_blank=True)
    aceite = serializers.BooleanField()

    def validate_nome(self, v):
        v = " ".join(v.split())
        if len(v.split(" ")) < 2:
            raise serializers.ValidationError("Informe nome e sobrenome (ou a razão social).")
        return v

    def validate_email(self, v):
        v = v.strip().lower()
        if Usuario.objects.filter(email__iexact=v).exists() or Usuario.objects.filter(username__iexact=v).exists():
            raise serializers.ValidationError("Este e-mail já está cadastrado. Tente entrar ou recuperar a senha.")
        return v

    def validate_documento(self, v):
        d = so_digitos(v)
        if not documento_valido(d):
            raise serializers.ValidationError("CPF ou CNPJ inválido.")
        if Usuario.objects.filter(documento=d).exists():
            raise serializers.ValidationError("Este CPF/CNPJ já possui cadastro.")
        return d

    def validate_telefone(self, v):
        if not telefone_valido(v):
            raise serializers.ValidationError("Telefone inválido. Use DDD + número, ex.: (84) 99999-1234.")
        return so_digitos(v)

    def validate_codigo_indicacao(self, v):
        if not v.strip():
            return None
        padrinho = _buscar_padrinho(v)
        if not padrinho:
            raise serializers.ValidationError("Código de indicação não encontrado.")
        return padrinho

    def validate_aceite(self, v):
        if not v:
            raise serializers.ValidationError("É preciso aceitar os termos de uso.")
        return v

    def validate(self, dados):
        usuario_tmp = Usuario(username=dados.get("email", ""), email=dados.get("email", ""), first_name=dados.get("nome", ""))
        try:
            validate_password(dados["password"], usuario_tmp)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"password": list(exc.messages)}) from exc
        padrinho = dados.get("codigo_indicacao")
        if padrinho and padrinho.documento and padrinho.documento == dados.get("documento"):
            raise serializers.ValidationError({"codigo_indicacao": "Você não pode usar o seu próprio código."})
        return dados


def _tokens(usuario):
    refresh = RefreshToken.for_user(usuario)
    return {"access": str(refresh.access_token), "refresh": str(refresh)}


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([ThrottlePublico])
def cadastro(request):
    ser = CadastroSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    d = ser.validated_data
    try:
        with transaction.atomic():
            usuario = Usuario(
                username=d["email"], email=d["email"], first_name=d["nome"], documento=d["documento"],
                telefone=d["telefone"], indicado_por=d.get("codigo_indicacao"),
            )
            usuario.set_password(d["password"])
            usuario.save()
    except IntegrityError:
        return Response({"detail": "E-mail ou CPF/CNPJ já cadastrado."}, status=status.HTTP_400_BAD_REQUEST)

    bonus = False
    if usuario.indicado_por_id and settings.BONUS_INDICACAO_GATILHO == "cadastro":
        bonus = servicos.pagar_bonus_indicacao(usuario.pk)
    return Response({**_tokens(usuario), "bonus_creditado": bonus}, status=status.HTTP_201_CREATED)


@api_view(["GET"])
@permission_classes([AllowAny])
@throttle_classes([ThrottlePublico])
def validar_codigo(request, codigo):
    padrinho = _buscar_padrinho(codigo)
    if not padrinho:
        return Response({"valido": False}, status=status.HTTP_404_NOT_FOUND)
    primeiro_nome = (padrinho.first_name or "").split(" ")[0] or "um cliente"
    return Response({"valido": True, "indicador": primeiro_nome, "codigo": padrinho.codigo_indicacao})


@api_view(["GET"])
@permission_classes([AllowAny])
def config_publica(request):
    return Response({
        "bonus_indicacao_centavos": settings.BONUS_INDICACAO_CENTAVOS,
        "bonus_gatilho": settings.BONUS_INDICACAO_GATILHO,
        "suporte_email": settings.SUPORTE_EMAIL,
        "checkout_ativo": infinitepay.configurado(),
    })


@api_view(["GET"])
def minhas_indicacoes(request):
    u = request.user
    indicados = u.indicados.order_by("-date_joined")
    recebido = (
        Movimentacao.objects.filter(usuario=u, tipo=Movimentacao.Tipo.BONUS, descricao__startswith="Bônus: indicação")
        .values_list("valor_centavos", flat=True)
    )
    return Response({
        "codigo": u.codigo_indicacao,
        "bonus_centavos": settings.BONUS_INDICACAO_CENTAVOS,
        "gatilho": settings.BONUS_INDICACAO_GATILHO,
        "total_indicados": indicados.count(),
        "total_recebido_centavos": sum(recebido),
        "indicados": [
            {
                "nome": (i.first_name or "Cliente").split(" ")[0],
                "desde": i.date_joined,
                "bonus_pago": i.bonus_indicacao_pago,
            }
            for i in indicados[:50]
        ],
    })


# --- Recuperação de senha -------------------------------------------------

MENSAGEM_ESQUECI = "Se este e-mail estiver cadastrado, você receberá um link para criar uma nova senha em instantes."


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([ThrottlePublico])
def esqueci_senha(request):
    email = str(request.data.get("email", "")).strip().lower()
    usuario = Usuario.objects.filter(email__iexact=email, is_active=True).first() if email else None
    if usuario and usuario.has_usable_password():
        uid = urlsafe_base64_encode(force_bytes(usuario.pk))
        token = default_token_generator.make_token(usuario)
        link = f"{settings.FRONTEND_URL}/redefinir-senha?uid={uid}&token={token}"
        try:
            enviar_redefinicao_senha(usuario, link)
        except Exception:  # noqa: BLE001 - não revelar ao público
            log.exception("Falha ao enviar e-mail de redefinição para usuário %s", usuario.pk)
    # Resposta sempre igual: não revela se o e-mail existe.
    return Response({"detail": MENSAGEM_ESQUECI, "suporte_email": settings.SUPORTE_EMAIL})


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([ThrottlePublico])
def redefinir_senha(request):
    invalido = Response(
        {"detail": "Link inválido ou expirado. Peça um novo em \"Esqueceu a senha?\"."},
        status=status.HTTP_400_BAD_REQUEST,
    )
    try:
        pk = force_str(urlsafe_base64_decode(str(request.data.get("uid", ""))))
        usuario = Usuario.objects.get(pk=pk, is_active=True)
    except (ValueError, TypeError, OverflowError, Usuario.DoesNotExist):
        return invalido
    if not default_token_generator.check_token(usuario, str(request.data.get("token", ""))):
        return invalido
    senha = str(request.data.get("password", ""))
    try:
        validate_password(senha, usuario)
    except DjangoValidationError as exc:
        return Response({"password": list(exc.messages)}, status=status.HTTP_400_BAD_REQUEST)
    usuario.set_password(senha)
    usuario.save(update_fields=["password"])
    return Response({"detail": "Senha alterada com sucesso. Você já pode entrar."})
