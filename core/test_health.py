from unittest.mock import patch

from django.db import OperationalError
from django.test import TestCase


class HealthTeste(TestCase):
    def test_banco_disponivel(self):
        response = self.client.get("/api/health/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_banco_indisponivel_sem_expor_detalhes(self):
        with patch("core.health.connection.cursor", side_effect=OperationalError("private connection details")):
            response = self.client.get("/api/health/")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "unavailable"})
