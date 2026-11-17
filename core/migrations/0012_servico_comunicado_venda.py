from django.db import migrations


def criar(apps, schema_editor):
    Servico = apps.get_model("core", "Servico")
    Servico.objects.get_or_create(codigo="comunicado-venda", defaults={
        "nome": "Comunicado de Venda",
        "descricao": "Comunica a venda do veículo ao órgão de trânsito e entrega o comprovante em PDF.",
        "fonte": "COMUNICADO",
        "formulario": "COMUNICADO_VENDA",
        "sku_fonte": "comunicacao-venda",
        "assincrono": False,
        # Começa desligado: defina os preços em Gestão > Serviços e preços e ative.
        "preco_centavos": 0,
        "ativo": False,
        "preco_publico_centavos": 0,
        "publico_ativo": False,
        "ordem": 3,
    })


class Migration(migrations.Migration):
    dependencies = [("core", "0011_comunicado_venda")]
    operations = [migrations.RunPython(criar, migrations.RunPython.noop)]
