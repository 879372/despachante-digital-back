from django.db import migrations

SERVICOS = [
    {
        "codigo": "crlv-rn",
        "nome": "CRLV-e RN",
        "descricao": "Emissão do CRLV digital (Rio Grande do Norte). Prazo estimado: 1 a 3 dias úteis.",
        "preco_centavos": 5000,
        "sku_fonte": "veicular-crlv-rn-sf",
        "assincrono": True,
        "ordem": 1,
    },
    {
        "codigo": "codigo-seguranca",
        "nome": "Código de Segurança do CRV",
        "descricao": "Consulta do código de segurança do CRV pela placa.",
        "preco_centavos": 3000,
        "sku_fonte": "veicular-codigo-seguranca-crv-sf",
        "assincrono": False,
        "ordem": 2,
    },
]


def criar(apps, schema_editor):
    Servico = apps.get_model("core", "Servico")
    for dados in SERVICOS:
        Servico.objects.update_or_create(codigo=dados["codigo"], defaults=dados)


class Migration(migrations.Migration):
    dependencies = [("core", "0001_initial")]
    operations = [migrations.RunPython(criar, migrations.RunPython.noop)]
