import secrets

from django.db import migrations


def preencher(apps, schema_editor):
    Usuario = apps.get_model("core", "Usuario")
    usados = set(Usuario.objects.exclude(codigo_indicacao=None).values_list("codigo_indicacao", flat=True))
    for u in Usuario.objects.filter(codigo_indicacao=None):
        base = "".join(c for c in (u.first_name or u.username).upper() if "A" <= c <= "Z")[:6] or "INDICA"
        codigo = f"{base}{secrets.randbelow(9000) + 1000}"
        while codigo in usados:
            codigo = f"{base}{secrets.randbelow(9000) + 1000}"
        usados.add(codigo)
        u.codigo_indicacao = codigo
        u.save(update_fields=["codigo_indicacao"])


class Migration(migrations.Migration):
    dependencies = [("core", "0003_indicacao")]
    operations = [migrations.RunPython(preencher, migrations.RunPython.noop)]
