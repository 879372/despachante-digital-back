"""Comunicado de venda: validação do formulário e montagem do envio ao órgão."""

import re
from datetime import date, datetime
from decimal import Decimal

import requests
from django.core.cache import cache
from rest_framework import serializers
from rest_framework.decorators import api_view, authentication_classes, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from .publico import ThrottleLeitura
from .validacoes import cnpj_valido, cpf_valido, so_digitos

UFS = {"AC", "AL", "AP", "AM", "BA", "CE", "DF", "ES", "GO", "MA", "MT", "MS", "MG", "PA", "PB", "PR", "PE", "PI",
       "RJ", "RN", "RS", "RO", "RR", "SC", "SP", "SE", "TO"}
MAX_ARQUIVO = 5 * 1024 * 1024


def _data(valor, campo, minimo=None):
    v = str(valor or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            d = datetime.strptime(v, fmt).date()
            break
        except ValueError:
            continue
    else:
        raise serializers.ValidationError({campo: "Data inválida. Use DD/MM/AAAA."})
    if d > date.today():
        raise serializers.ValidationError({campo: "A data não pode estar no futuro."})
    if minimo and d < minimo:
        raise serializers.ValidationError({campo: "Data muito antiga."})
    return d


class PessoaSerializer(serializers.Serializer):
    tipo_pessoa = serializers.ChoiceField(choices=["F", "J"])
    documento = serializers.CharField(max_length=18)
    nome = serializers.CharField(min_length=3, max_length=120)

    def validate(self, d):
        doc = so_digitos(d["documento"])
        if d["tipo_pessoa"] == "F" and not cpf_valido(doc):
            raise serializers.ValidationError({"documento": "CPF inválido."})
        if d["tipo_pessoa"] == "J" and not cnpj_valido(doc):
            raise serializers.ValidationError({"documento": "CNPJ inválido."})
        d["documento"] = doc
        d["nome"] = " ".join(d["nome"].split()).upper()
        return d


class EnderecoSerializer(serializers.Serializer):
    cep = serializers.CharField(max_length=9)
    logradouro = serializers.CharField(max_length=120)
    numero = serializers.CharField(max_length=10)
    bairro = serializers.CharField(max_length=80)
    complemento = serializers.CharField(max_length=60, required=False, allow_blank=True)
    cidade_ibge = serializers.CharField(max_length=7)
    cidade_nome = serializers.CharField(max_length=80, required=False, allow_blank=True)

    def validate_cep(self, v):
        v = so_digitos(v)
        if len(v) != 8:
            raise serializers.ValidationError("CEP inválido.")
        return v

    def validate_cidade_ibge(self, v):
        v = so_digitos(v)
        if len(v) != 7:
            raise serializers.ValidationError("Cidade não identificada. Confira o CEP.")
        return v


class ArquivoSerializer(serializers.Serializer):
    nome = serializers.CharField(max_length=120)
    base64 = serializers.CharField(max_length=4 * ((MAX_ARQUIVO + 2) // 3))
    mime_type = serializers.ChoiceField(choices=["application/pdf", "image/jpeg", "image/png"])


class ComunicadoVendaSerializer(serializers.Serializer):
    vendedor = PessoaSerializer()
    comprador = PessoaSerializer()
    endereco_comprador = EnderecoSerializer()
    venda_data = serializers.CharField()
    venda_valor = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("1.00"))
    venda_cidade_ibge = serializers.CharField(max_length=7, required=False, allow_blank=True)
    comprador_solicitante = serializers.BooleanField(default=False)
    renavam = serializers.CharField(max_length=11)
    ano_fabricacao = serializers.IntegerField()
    ano_modelo = serializers.IntegerField()
    crv_numero = serializers.CharField(max_length=20)
    crv_codigo_seguranca = serializers.CharField(max_length=20)
    crv_via = serializers.IntegerField(min_value=1, max_value=99)
    crv_data_emissao = serializers.CharField()
    crv_uf = serializers.CharField(max_length=2)
    arquivos = ArquivoSerializer(many=True, required=False)

    def validate_renavam(self, v):
        v = so_digitos(v)
        if not 9 <= len(v) <= 11:
            raise serializers.ValidationError("Renavam inválido.")
        v = v.zfill(11)
        base, dv = v[:10], int(v[10])
        soma = sum(int(n) * p for n, p in zip(base[::-1], [2, 3, 4, 5, 6, 7, 8, 9, 2, 3]))
        if (soma * 10) % 11 % 10 != dv:
            raise serializers.ValidationError("Renavam inválido (dígito verificador).")
        return v

    def validate_crv_numero(self, v):
        v = so_digitos(v)
        if not v:
            raise serializers.ValidationError("Informe o número do CRV.")
        return v

    def validate_crv_codigo_seguranca(self, v):
        v = so_digitos(v)
        if len(v) < 8:
            raise serializers.ValidationError("Código de segurança do CRV inválido.")
        return v

    def validate_crv_uf(self, v):
        v = v.upper().strip()
        if v not in UFS:
            raise serializers.ValidationError("UF inválida.")
        return v

    def validate_venda_cidade_ibge(self, v):
        v = so_digitos(v)
        if v and len(v) != 7:
            raise serializers.ValidationError("Cidade da venda não identificada.")
        return v

    def validate_arquivos(self, v):
        if len(v) > 2:
            raise serializers.ValidationError("Envie no máximo 2 arquivos.")
        import base64 as b64
        for a in v:
            try:
                tamanho = len(b64.b64decode(a["base64"], validate=True))
            except ValueError as exc:
                raise serializers.ValidationError("Arquivo corrompido.") from exc
            if tamanho == 0:
                raise serializers.ValidationError("O arquivo está vazio.")
            if tamanho > MAX_ARQUIVO:
                raise serializers.ValidationError("Cada arquivo pode ter até 5 MB.")
        return v

    def validate(self, d):
        ano = date.today().year
        if not 1900 <= d["ano_fabricacao"] <= ano:
            raise serializers.ValidationError({"ano_fabricacao": "Ano de fabricação inválido."})
        if not d["ano_fabricacao"] <= d["ano_modelo"] <= ano + 1:
            raise serializers.ValidationError({"ano_modelo": "Ano do modelo inválido."})
        emissao = _data(d["crv_data_emissao"], "crv_data_emissao")
        venda = _data(d["venda_data"], "venda_data", minimo=emissao)
        if d["vendedor"]["documento"] == d["comprador"]["documento"]:
            raise serializers.ValidationError({"comprador": "Comprador e vendedor não podem ser a mesma pessoa."})
        d["crv_data_emissao"] = emissao.strftime("%d/%m/%Y")
        d["venda_data"] = venda.strftime("%d/%m/%Y")
        d["venda_valor"] = str(d["venda_valor"])
        d["venda_cidade_ibge"] = d.get("venda_cidade_ibge") or d["endereco_comprador"]["cidade_ibge"]
        return d


def montar_payload(dados, placa, url_notificacao=""):
    """Converte o formulário validado para o formato de envio."""
    import base64 as b64
    import hashlib

    def pessoa(p):
        base = {"tipo_pessoa": p["tipo_pessoa"], "nome": p["nome"]}
        base["cpf" if p["tipo_pessoa"] == "F" else "cnpj"] = p["documento"]
        return base

    end = dados["endereco_comprador"]
    payload = {
        "vendedor": pessoa(dados["vendedor"]),
        "comprador": {**pessoa(dados["comprador"]), "endereco": {
            "cep": end["cep"], "logradouro": end["logradouro"], "numero": end["numero"], "bairro": end["bairro"],
            "complemento": end.get("complemento", ""), "cidade": int(end["cidade_ibge"]),
        }},
        "venda": {
            "cidade": int(dados["venda_cidade_ibge"]), "data": dados["venda_data"], "valor": float(dados["venda_valor"]),
            "comprador_solicitante": "S" if dados.get("comprador_solicitante") else "N",
        },
        "veiculo": {
            "placa": placa, "renavam": dados["renavam"], "ano_fabricacao": dados["ano_fabricacao"],
            "ano_modelo": dados["ano_modelo"],
            "crv": {"numero": dados["crv_numero"], "codigo_seguranca": dados["crv_codigo_seguranca"],
                    "numero_via": dados["crv_via"], "data_emissao": dados["crv_data_emissao"], "uf_emissao": dados["crv_uf"]},
        },
    }
    arquivos = []
    for a in dados.get("arquivos") or []:
        conteudo = b64.b64decode(a["base64"])
        ext = {"application/pdf": "pdf", "image/jpeg": "jpg", "image/png": "png"}[a["mime_type"]]
        arquivos.append({"nome": a["nome"][:120], "bytes": a["base64"], "tamanho": len(conteudo), "extensao": ext,
                         "mime_type": a["mime_type"], "hash": hashlib.md5(conteudo).hexdigest()})  # noqa: S324 (exigido pela API)
    if arquivos:
        payload["arquivos"] = arquivos
    if url_notificacao:
        payload["url_notificacao"] = url_notificacao
    return payload


def resumo(dados):
    """O que pode ser mostrado de volta ao dono do pedido (sem código de segurança, documentos mascarados)."""
    if not dados:
        return None

    def mascara(doc):
        return f"{doc[:3]}.***.***-{doc[-2:]}" if len(doc) == 11 else f"{doc[:2]}.***.***/****-{doc[-2:]}"

    end = dados.get("endereco_comprador") or {}
    return {
        "vendedor": f"{dados['vendedor']['nome']} · {mascara(dados['vendedor']['documento'])}",
        "comprador": f"{dados['comprador']['nome']} · {mascara(dados['comprador']['documento'])}",
        "cidade": f"{end.get('cidade_nome') or ''}".strip(),
        "data_venda": dados.get("venda_data"),
        "valor_venda": dados.get("venda_valor"),
        "renavam": f"*******{dados.get('renavam', '')[-4:]}",
    }


def sem_segredos(dados):
    """Versão para guardar depois de transmitido: sem código de segurança do CRV nem arquivos."""
    if not dados:
        return dados
    limpo = {k: v for k, v in dados.items() if k not in ("crv_codigo_seguranca", "arquivos")}
    return limpo


# --- CEP -> endereço + código IBGE ------------------------------------------------

@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([ThrottleLeitura])
def cep(request, valor):
    cep_ = so_digitos(valor)
    if len(cep_) != 8:
        return Response({"detail": "CEP inválido."}, status=400)
    chave = f"cep:{cep_}"
    if (salvo := cache.get(chave)) is not None:
        return Response(salvo) if salvo else Response({"detail": "CEP não encontrado."}, status=404)
    try:
        r = requests.get(f"https://viacep.com.br/ws/{cep_}/json/", timeout=8)
        d = r.json()
        if r.status_code != 200 or not isinstance(d, dict):
            return Response({"detail": "Não foi possível consultar o CEP agora. Preencha o endereço manualmente."}, status=503)
    except (requests.RequestException, ValueError):
        return Response({"detail": "Não foi possível consultar o CEP agora. Preencha o endereço manualmente."}, status=503)
    if d.get("erro") or not re.fullmatch(r"\d{7}", str(d.get("ibge", ""))):
        cache.set(chave, {}, 3600)
        return Response({"detail": "CEP não encontrado."}, status=404)
    salvo = {"cep": cep_, "logradouro": d.get("logradouro", ""), "bairro": d.get("bairro", ""),
             "cidade_nome": d.get("localidade", ""), "uf": d.get("uf", ""), "cidade_ibge": d["ibge"]}
    cache.set(chave, salvo, 86400)
    return Response(salvo)


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([ThrottleLeitura])
def retorno(request, token, pk):
    """A notificação apenas dispara consulta autenticada: seu corpo não altera o pedido."""
    import secrets
    from django.conf import settings
    from .models import Pedido, Servico
    from .servicos import sincronizar_pedido

    esperado = settings.CARVEN["WEBHOOK_TOKEN"]
    if not esperado or not secrets.compare_digest(token, esperado):
        return Response(status=404)
    pedido = Pedido.objects.select_related("servico").filter(pk=pk, servico__fonte=Servico.Fonte.COMUNICADO).first()
    if pedido and pedido.protocolo_fonte:
        sincronizar_pedido(pedido)
    return Response({"recebido": True})
