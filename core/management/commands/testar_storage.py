import uuid

from django.core.files.base import ContentFile
from django.core.files.storage import storages
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Confere o armazenamento de arquivos (S3 ou disco): grava, lê e apaga um arquivo de teste."

    def handle(self, *args, **opts):
        default_storage = storages["default"]
        backend = type(default_storage).__name__
        destino = getattr(default_storage, "bucket_name", None) or getattr(default_storage, "location", "")
        self.stdout.write(f"Armazenamento: {backend} · {destino}")
        nome = f"_teste/{uuid.uuid4().hex}.txt"
        conteudo = b"teste de armazenamento"
        try:
            salvo = default_storage.save(nome, ContentFile(conteudo))
            self.stdout.write(f"  gravou   {salvo}")
            with default_storage.open(salvo, "rb") as f:
                if f.read() != conteudo:
                    raise CommandError("Conteúdo lido difere do gravado.")
            self.stdout.write("  leu      ok")
            if hasattr(default_storage, "bucket_name"):
                url = default_storage.url(salvo)
                self.stdout.write(f"  URL assinada expira: {'X-Amz-Expires' in url or 'Expires' in url}")
            default_storage.delete(salvo)
            self.stdout.write("  apagou   ok")
        except CommandError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise CommandError(f"Falha: {type(exc).__name__}: {exc}") from exc
        self.stdout.write(self.style.SUCCESS("Armazenamento funcionando."))
