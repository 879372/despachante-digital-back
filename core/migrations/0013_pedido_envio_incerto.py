from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0012_servico_comunicado_venda")]
    operations = [migrations.AddField(
        model_name="pedido", name="envio_incerto",
        field=models.BooleanField(default=False, help_text="Envio sem resposta conclusiva; requer conferência antes de nova inserção"),
    )]
