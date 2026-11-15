from django.db import migrations


def preencher(apps, schema_editor):
    Servico = apps.get_model("core", "Servico")
    for s in Servico.objects.all():
        if not s.preco_publico_centavos:
            s.preco_publico_centavos = s.preco_centavos
            s.publico_ativo = s.ativo
            s.save(update_fields=["preco_publico_centavos", "publico_ativo"])


class Migration(migrations.Migration):
    dependencies = [("core", "0009_site_publico")]
    operations = [migrations.RunPython(preencher, migrations.RunPython.noop)]
