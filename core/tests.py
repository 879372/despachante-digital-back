import base64
import hashlib
import hmac
import json
import time
from unittest import mock

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from .integracao.cliente import ErroFonte, assinatura_valida, higienizar
from .models import Conversa, Mensagem, Movimentacao, Pedido, Servico, Usuario
from .servicos import _registrar_pagamento, sincronizar_pedido

PDF = base64.b64encode(b"%PDF-1.4 teste").decode()


class FonteFalsa:
    def __init__(self, consulta=None, emissao=None, status=None, erro=None):
        self._consulta, self._emissao, self._status, self._erro = consulta, emissao, status, erro
        self.chamadas = []

    def consultar(self, sku, placa, idem):
        self.chamadas.append(("consultar", sku, placa, idem))
        if self._erro:
            raise self._erro
        return 200, self._consulta

    def solicitar_emissao(self, sku, placa, idem):
        self.chamadas.append(("emissao", sku, placa, idem))
        if self._erro:
            raise self._erro
        return 202, self._emissao

    def status(self, protocolo):
        self.chamadas.append(("status", protocolo))
        return 200, self._status


class BaseTeste(TestCase):
    def setUp(self):
        self.u = Usuario.objects.create_user("cliente", password="senha-forte-123", saldo_centavos=10000)
        self.api = APIClient()
        self.api.force_authenticate(self.u)
        self.crlv = Servico.objects.get(codigo="crlv-rn")
        self.crv = Servico.objects.get(codigo="codigo-seguranca")

    def pedir(self, servico, fonte, placa="abc-1d23"):
        with mock.patch("core.servicos.ClienteFonte", return_value=fonte):
            return self.api.post("/api/pedidos/", {"servico": servico, "placa": placa}, format="json")


class CodigoSegurancaTeste(BaseTeste):
    def test_sucesso_debita_e_esconde_fonte(self):
        fonte = FonteFalsa(consulta={"resultado": {
            "familia": "veicular", "encontrado": True,
            "dados": {"placa": "ABC1D23", "codigoSeguranca": "12345678901", "provedor": "X", "fonte": "Y"},
        }, "cobrado": True, "valorCobradoCentavos": 900})
        r = self.pedir("codigo-seguranca", fonte)
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.data["codigo_seguranca"], "12345678901")
        corpo = json.dumps(r.data, default=str)
        for proibido in ("provedor", "sku", "veicular-codigo", "buscars", "custo", "cobrad"):
            self.assertNotIn(proibido, corpo)
        self.assertEqual(fonte.chamadas[0][1], "veicular-codigo-seguranca-crv-sf")
        self.assertEqual(fonte.chamadas[0][2], "ABC1D23")
        self.u.refresh_from_db()
        self.assertEqual(self.u.saldo_centavos, 7000)
        self.assertEqual(Pedido.objects.get().custo_centavos, 900)

    def test_saldo_insuficiente(self):
        self.u.saldo_centavos = 100
        self.u.save()
        fonte = FonteFalsa()
        r = self.pedir("codigo-seguranca", fonte)
        self.assertEqual(r.status_code, 402)
        self.assertEqual(fonte.chamadas, [])

    def test_placa_invalida(self):
        r = self.pedir("codigo-seguranca", FonteFalsa(), placa="12")
        self.assertEqual(r.status_code, 400)

    def test_erro_definitivo_estorna_com_mensagem_generica(self):
        fonte = FonteFalsa(erro=ErroFonte("Provedor XPTO caiu", status_http=422))
        r = self.pedir("codigo-seguranca", fonte)
        self.assertEqual(r.data["status"], "FALHA")
        self.assertNotIn("XPTO", r.data["mensagem"])
        self.u.refresh_from_db()
        self.assertEqual(self.u.saldo_centavos, 10000)

    def test_erro_temporario_fica_pendente_para_reenvio(self):
        fonte = FonteFalsa(erro=ErroFonte("rede", rede=True))
        r = self.pedir("codigo-seguranca", fonte)
        self.assertEqual(r.status_code, 202)
        self.assertEqual(r.data["status"], "PENDENTE")
        pedido = Pedido.objects.get()
        ok = FonteFalsa(consulta={"resultado": {"encontrado": True, "dados": {"codigo_seguranca": "999"}}})
        sincronizar_pedido(pedido, ok, forcar=True)
        pedido.refresh_from_db()
        self.assertEqual(pedido.status, "SUCESSO")
        self.assertEqual(fonte.chamadas[0][3], ok.chamadas[0][3])  # mesma idempotency-key


class CrlvTeste(BaseTeste):
    def test_fluxo_assincrono_com_pdf(self):
        fonte = FonteFalsa(emissao={"consultaId": "cns_1", "status": "PENDENTE"})
        r = self.pedir("crlv-rn", fonte)
        self.assertEqual(r.status_code, 202)
        self.assertEqual(r.data["status"], "PENDENTE")
        self.assertNotIn("cns_1", json.dumps(r.data, default=str))
        self.assertEqual(fonte.chamadas[0][1], "veicular-crlv-rn-sf")

        pedido = Pedido.objects.get()
        pronto = FonteFalsa(status={"status": "SUCESSO", "cobrado": True, "resultado": {
            "documento": {"base64": PDF, "formato": "pdf", "bytes": 14}}})
        sincronizar_pedido(pedido, pronto, forcar=True)
        pedido.refresh_from_db()
        self.assertEqual(pedido.status, "SUCESSO")
        self.assertTrue(pedido.documento)

        r = self.api.get(f"/api/pedidos/{pedido.pk}/documento/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(b"".join(r.streaming_content), b"%PDF-1.4 teste")

    def test_falha_estorna_uma_unica_vez(self):
        fonte = FonteFalsa(emissao={"consultaId": "cns_2", "status": "PENDENTE"})
        self.pedir("crlv-rn", fonte)
        pedido = Pedido.objects.get()
        falha = FonteFalsa(status={"status": "FALHA", "cobrado": False})
        sincronizar_pedido(pedido, falha, forcar=True)
        sincronizar_pedido(pedido, falha, forcar=True)
        self.u.refresh_from_db()
        self.assertEqual(self.u.saldo_centavos, 10000)
        self.assertEqual(Movimentacao.objects.filter(tipo="ESTORNO").count(), 1)

    def test_outro_usuario_nao_ve_pedido(self):
        self.pedir("crlv-rn", FonteFalsa(emissao={"consultaId": "c", "status": "PENDENTE"}))
        outro = APIClient()
        outro.force_authenticate(Usuario.objects.create_user("outro", password="x-senha-123"))
        pedido = Pedido.objects.get()
        self.assertEqual(outro.get(f"/api/pedidos/{pedido.pk}/").status_code, 404)

    def test_servicos_publicos_sem_sku(self):
        r = self.api.get("/api/servicos/")
        self.assertNotIn("veicular", json.dumps(r.data))


@override_settings(FONTE_DADOS={"URL": "http://x", "CHAVE": "k", "SEGREDO_WEBHOOK": "seg", "TIMEOUT": 5},
                   WEBHOOK_TOKEN_ROTA="tok")
class WebhookTeste(BaseTeste):
    def _assinar(self, corpo, ts):
        return "sha256=" + hmac.new(b"seg", ts.encode() + b"." + corpo, hashlib.sha256).hexdigest()

    def test_assinatura(self):
        ts = str(int(time.time()))
        self.assertTrue(assinatura_valida(b"{}", ts, self._assinar(b"{}", ts), "seg"))
        self.assertFalse(assinatura_valida(b"{}", ts, "sha256=errado", "seg"))
        velho = str(int(time.time()) - 3600)
        self.assertFalse(assinatura_valida(b"{}", velho, self._assinar(b"{}", velho), "seg"))

    def test_webhook_dispara_sincronizacao(self):
        self.pedir("crlv-rn", FonteFalsa(emissao={"consultaId": "cns_9", "status": "PENDENTE"}))
        corpo = json.dumps({"evento": "consulta.sucesso", "statusUrl": "/v1/consultas/cns_9/status"}).encode()
        ts = str(int(time.time()))
        pronto = FonteFalsa(status={"status": "SUCESSO", "resultado": {"documento": {"base64": PDF}}})
        with mock.patch("core.servicos.ClienteFonte", return_value=pronto):
            r = self.client.post("/api/retorno/tok/", corpo, content_type="application/json",
                                 HTTP_X_BUSCARS_TIMESTAMP=ts, HTTP_X_BUSCARS_SIGNATURE=self._assinar(corpo, ts))
        self.assertEqual(r.status_code, 204)
        self.assertEqual(Pedido.objects.get().status, "SUCESSO")

    def test_webhook_assinatura_invalida(self):
        r = self.client.post("/api/retorno/tok/", b"{}", content_type="application/json",
                             HTTP_X_BUSCARS_TIMESTAMP=str(int(time.time())), HTTP_X_BUSCARS_SIGNATURE="sha256=x")
        self.assertEqual(r.status_code, 401)

    def test_token_errado_404(self):
        self.assertEqual(self.client.post("/api/retorno/outro/", b"{}", content_type="application/json").status_code, 404)


class CarteiraTeste(BaseTeste):
    def test_pix_manual_nao_existe_mais(self):
        self.assertEqual(self.api.post("/api/carteira/recargas/", {"valor_centavos": 5000}, format="json").status_code, 404)
        self.assertEqual(self.api.get("/api/carteira/pix/").status_code, 404)

    def test_ajuste_manual_da_equipe(self):
        admin = APIClient()
        admin.force_authenticate(Usuario.objects.create_user("admin", password="x-senha-123", is_staff=True))
        r = admin.post(f"/api/admin/usuarios/{self.u.pk}/ajuste/", {"valor_centavos": 500, "descricao": "Cortesia"}, format="json")
        self.assertEqual(r.data["saldo_centavos"], 10500)

    def test_cliente_nao_acessa_admin(self):
        self.assertEqual(self.api.get("/api/admin/resumo/").status_code, 403)


class HigienizarTeste(TestCase):
    def test_remove_chaves(self):
        self.assertEqual(higienizar({"a": 1, "provedor": "x", "sub": [{"fonteNome": "y", "b": "Buscars ok"}]}),
                         {"a": 1, "sub": [{"b": " ok"}]})


# ---------------------------------------------------------------------------
# Cadastro, indicação e senha
# ---------------------------------------------------------------------------
from django.core import mail  # noqa: E402
from django.core.cache import cache  # noqa: E402

from .validacoes import cnpj_valido, cpf_valido, telefone_valido  # noqa: E402

CPF_A, CPF_B, CNPJ = "529.982.247-25", "111.444.777-35", "11.222.333/0001-81"


def dados_cadastro(**extra):
    base = {"nome": "Maria Souza", "email": "maria@exemplo.com", "documento": CPF_B, "telefone": "(84) 99999-1234",
            "password": "Senha-forte-2026", "aceite": True}
    base.update(extra)
    return base


class ValidacoesTeste(TestCase):
    def test_documentos(self):
        self.assertTrue(cpf_valido(CPF_A))
        self.assertFalse(cpf_valido("123.456.789-00"))
        self.assertTrue(cnpj_valido(CNPJ))
        self.assertFalse(cnpj_valido("11.222.333/0001-80"))
        self.assertTrue(telefone_valido("84999991234"))
        self.assertFalse(telefone_valido("8499999"))


class CadastroTeste(TestCase):
    def setUp(self):
        cache.clear()
        self.padrinho = Usuario.objects.create_user("joao", email="joao@x.com", password="x-senha-123",
                                                    first_name="João Lima", documento="52998224725")

    def test_codigo_gerado(self):
        self.assertTrue(self.padrinho.codigo_indicacao.startswith("JOAO"))

    def test_cadastro_ok_e_login_por_email(self):
        r = self.client.post("/api/auth/cadastro/", dados_cadastro(), content_type="application/json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertIn("access", r.json())
        u = Usuario.objects.get(email="maria@exemplo.com")
        self.assertEqual(u.documento, "11144477735")
        self.assertEqual(u.telefone, "84999991234")
        r = self.client.post("/api/auth/token/", {"username": "MARIA@exemplo.com", "password": "Senha-forte-2026"},
                             content_type="application/json")
        self.assertEqual(r.status_code, 200)

    def test_validacoes(self):
        r = self.client.post("/api/auth/cadastro/", dados_cadastro(documento="123.456.789-00", telefone="12",
                             nome="Maria", password="123", aceite=False), content_type="application/json")
        self.assertEqual(r.status_code, 400)
        for campo in ("documento", "telefone", "nome", "aceite"):
            self.assertIn(campo, r.json())

    def test_duplicados(self):
        r = self.client.post("/api/auth/cadastro/", dados_cadastro(email="JOAO@x.com", documento=CPF_A),
                             content_type="application/json")
        self.assertIn("email", r.json())
        self.assertIn("documento", r.json())

    def test_codigo_invalido(self):
        r = self.client.post("/api/auth/cadastro/", dados_cadastro(codigo_indicacao="NAOEXISTE"), content_type="application/json")
        self.assertIn("codigo_indicacao", r.json())

    def test_validar_codigo_publico(self):
        r = self.client.get(f"/api/auth/indicacao/{self.padrinho.codigo_indicacao.lower()}/")
        self.assertEqual(r.json(), {"valido": True, "indicador": "João", "codigo": self.padrinho.codigo_indicacao})
        self.assertEqual(self.client.get("/api/auth/indicacao/XYZ/").status_code, 404)

    @override_settings(BONUS_INDICACAO_GATILHO="primeira_recarga", BONUS_INDICACAO_CENTAVOS=500)
    def test_bonus_na_primeira_recarga_uma_unica_vez(self):
        self.client.post("/api/auth/cadastro/", dados_cadastro(codigo_indicacao=self.padrinho.codigo_indicacao),
                         content_type="application/json")
        maria = Usuario.objects.get(email="maria@exemplo.com")
        self.assertEqual(maria.saldo_centavos, 0)
        from .models import Cobranca
        for _ in range(2):
            c = Cobranca.objects.create(usuario=maria, tipo="RECARGA", valor_centavos=2000)
            _registrar_pagamento(c.pk, {"paid": True, "amount": 2000, "capture_method": "pix"})
        maria.refresh_from_db()
        self.padrinho.refresh_from_db()
        self.assertEqual(maria.saldo_centavos, 4000 + 500)
        self.assertEqual(self.padrinho.saldo_centavos, 500)

    @override_settings(BONUS_INDICACAO_GATILHO="cadastro", BONUS_INDICACAO_CENTAVOS=500)
    def test_bonus_no_cadastro(self):
        r = self.client.post("/api/auth/cadastro/", dados_cadastro(codigo_indicacao=self.padrinho.codigo_indicacao),
                             content_type="application/json")
        self.assertTrue(r.json()["bonus_creditado"])
        self.padrinho.refresh_from_db()
        self.assertEqual(self.padrinho.saldo_centavos, 500)
        self.assertEqual(Usuario.objects.get(email="maria@exemplo.com").saldo_centavos, 500)
        api = APIClient()
        api.force_authenticate(self.padrinho)
        dados = api.get("/api/indicacoes/").json()
        self.assertEqual(dados["total_indicados"], 1)
        self.assertEqual(dados["total_recebido_centavos"], 500)
        self.assertNotIn("email", json.dumps(dados))

    def test_limite_publico(self):
        codigos = [self.client.get("/api/auth/indicacao/XYZ/").status_code for _ in range(12)]
        self.assertIn(429, codigos)


@override_settings(FRONTEND_URL="http://app.teste", SUPORTE_EMAIL="suporte@teste.com")
class SenhaTeste(TestCase):
    def setUp(self):
        cache.clear()
        self.u = Usuario.objects.create_user("ana", email="ana@x.com", password="senha-antiga-123")

    def test_fluxo_completo(self):
        r = self.client.post("/api/auth/senha/esqueci/", {"email": "ANA@x.com"}, content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("kaibez.com.br", mail.outbox[0].from_email)
        self.assertEqual(mail.outbox[0].alternatives[0][1], "text/html")
        self.assertIn("Criar nova senha", mail.outbox[0].alternatives[0][0])
        corpo = mail.outbox[0].body
        self.assertIn("suporte@teste.com", corpo)
        link = next(linha for linha in corpo.splitlines() if linha.startswith("http://app.teste/redefinir-senha"))
        from urllib.parse import parse_qs, urlparse
        q = parse_qs(urlparse(link).query)
        r = self.client.post("/api/auth/senha/redefinir/", {"uid": q["uid"][0], "token": q["token"][0], "password": "Nova-senha-2026"},
                             content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content)
        self.u.refresh_from_db()
        self.assertTrue(self.u.check_password("Nova-senha-2026"))
        # link não pode ser reutilizado
        r = self.client.post("/api/auth/senha/redefinir/", {"uid": q["uid"][0], "token": q["token"][0], "password": "Outra-senha-2026"},
                             content_type="application/json")
        self.assertEqual(r.status_code, 400)

    @override_settings(SUPORTE_EMAIL="")
    def test_sem_suporte_avisa_que_nao_deve_responder(self):
        self.client.post("/api/auth/senha/esqueci/", {"email": "ana@x.com"}, content_type="application/json")
        msg = mail.outbox[0]
        self.assertIn("não responda", msg.body)
        self.assertIn("chat do sistema", msg.alternatives[0][0])
        self.assertEqual(msg.reply_to, [])

    def test_email_inexistente_mesma_resposta(self):
        r1 = self.client.post("/api/auth/senha/esqueci/", {"email": "nao@existe.com"}, content_type="application/json")
        r2 = self.client.post("/api/auth/senha/esqueci/", {"email": "ana@x.com"}, content_type="application/json")
        self.assertEqual(r1.json()["detail"], r2.json()["detail"])
        self.assertEqual(len(mail.outbox), 1)


# ---------------------------------------------------------------------------
# Chat interno
# ---------------------------------------------------------------------------
from django.core.files.uploadedfile import SimpleUploadedFile  # noqa: E402


class ChatTeste(TestCase):
    def setUp(self):
        cache.clear()
        self.cli = Usuario.objects.create_user("cli", password="x-senha-123", first_name="Carla Dias")
        self.outro = Usuario.objects.create_user("outro", password="x-senha-123")
        self.eq = Usuario.objects.create_user("eq", password="x-senha-123", first_name="Pedro Equipe", is_staff=True)
        self.c, self.o, self.e = APIClient(), APIClient(), APIClient()
        self.c.force_authenticate(self.cli)
        self.o.force_authenticate(self.outro)
        self.e.force_authenticate(self.eq)

    def abrir(self, **extra):
        return self.c.post("/api/chat/conversas/", {"texto": "Meu CRLV não chegou", **extra}, format="json")

    def test_fluxo_completo_e_nao_lidas(self):
        r = self.abrir()
        self.assertEqual(r.status_code, 201, r.content)
        cid = r.data["id"]
        self.assertEqual(r.data["assunto"], "Meu CRLV não chegou")
        self.assertEqual(self.e.get("/api/chat/nao-lidas/").data["total"], 1)
        self.assertEqual(self.c.get("/api/chat/nao-lidas/").data["total"], 0)

        lista = self.e.get("/api/chat/conversas/").data
        self.assertEqual(lista[0]["cliente"]["nome"], "Carla Dias")
        self.assertNotIn("cliente", self.c.get("/api/chat/conversas/").data[0])

        r = self.e.get(f"/api/chat/conversas/{cid}/mensagens/")
        self.assertEqual(len(r.data["mensagens"]), 1)
        self.assertEqual(self.e.get("/api/chat/nao-lidas/").data["total"], 0)  # equipe leu

        r = self.e.post(f"/api/chat/conversas/{cid}/mensagens/", {"texto": "Vou verificar!"}, format="json")
        self.assertEqual(r.data["autor"], "Você")
        self.assertEqual(self.c.get("/api/chat/nao-lidas/").data["total"], 1)
        r = self.c.get(f"/api/chat/conversas/{cid}/mensagens/")
        self.assertEqual(r.data["mensagens"][-1]["autor"], "Equipe · Pedro")
        self.assertEqual(self.c.get("/api/chat/nao-lidas/").data["total"], 0)

        ultimo = r.data["mensagens"][-1]["id"]
        self.assertEqual(self.c.get(f"/api/chat/conversas/{cid}/mensagens/?depois={ultimo}").data["mensagens"], [])

    def test_isolamento_entre_clientes(self):
        cid = self.abrir().data["id"]
        self.assertEqual(self.o.get(f"/api/chat/conversas/{cid}/mensagens/").status_code, 404)
        self.assertEqual(self.o.post(f"/api/chat/conversas/{cid}/mensagens/", {"texto": "oi"}, format="json").status_code, 404)
        self.assertEqual(self.o.get("/api/chat/conversas/").data, [])

    def test_resolver_e_reabrir(self):
        cid = self.abrir().data["id"]
        self.e.post(f"/api/chat/conversas/{cid}/status/", {"status": "RESOLVIDA"}, format="json")
        self.c.post(f"/api/chat/conversas/{cid}/mensagens/", {"texto": "Ainda preciso de ajuda"}, format="json")
        self.assertEqual(Conversa.objects.get(pk=cid).status, "ABERTA")

    def test_pedido_vinculado_so_do_proprio_cliente(self):
        servico = Servico.objects.get(codigo="crlv-rn")
        meu = Pedido.objects.create(usuario=self.cli, servico=servico, placa="ABC1D23", valor_centavos=1, chave_idempotencia="a")
        alheio = Pedido.objects.create(usuario=self.outro, servico=servico, placa="XYZ9A88", valor_centavos=1, chave_idempotencia="b")
        r = self.abrir(pedido=str(meu.pk))
        self.assertEqual(r.data["pedido"]["placa"], "ABC1D23")
        self.assertIn("ABC1D23", r.data["assunto"])
        self.assertEqual(self.abrir(pedido=str(alheio.pk)).status_code, 400)

    def test_validacoes(self):
        self.assertEqual(self.c.post("/api/chat/conversas/", {"texto": "  "}, format="json").status_code, 400)
        exe = SimpleUploadedFile("virus.exe", b"x")
        self.assertEqual(self.c.post("/api/chat/conversas/", {"texto": "a", "anexo": exe}).status_code, 400)
        self.assertEqual(self.e.post("/api/chat/conversas/", {"texto": "oi"}, format="json").status_code, 400)

    def test_anexo_protegido(self):
        img = SimpleUploadedFile("comprovante.png", b"\x89PNG teste", content_type="image/png")
        cid = self.c.post("/api/chat/conversas/", {"texto": "segue", "anexo": img}).data["id"]
        msg = self.c.get(f"/api/chat/conversas/{cid}/mensagens/").data["mensagens"][0]
        self.assertEqual(msg["anexo"]["nome"], "comprovante.png")
        url = "/api" + msg["anexo"]["url"]
        self.assertEqual(self.c.get(url).status_code, 200)
        self.assertEqual(self.e.get(url).status_code, 200)
        self.assertEqual(self.o.get(url).status_code, 404)


# ---------------------------------------------------------------------------
# Pagamento online (InfinitePay)
# ---------------------------------------------------------------------------
from .models import Cobranca  # noqa: E402

IP = {"HANDLE": "minha-loja", "API_KEY": "inchk_teste", "WEBHOOK_BASE": "https://api.teste", "WEBHOOK_TOKEN": "tokpg",
      "VALIDADE_HORAS": 2}


class RespostaFalsa:
    def __init__(self, status, dados):
        self.status_code, self._dados, self.content = status, dados, b"x"

    def json(self):
        return self._dados


@override_settings(INFINITEPAY=IP, FRONTEND_URL="http://app.teste", BONUS_INDICACAO_GATILHO="primeira_recarga")
class PagamentoTeste(TestCase):
    def setUp(self):
        cache.clear()
        self.u = Usuario.objects.create_user("pg", password="x-senha-123", first_name="Paula Gomes",
                                             email="paula@x.com", telefone="84999991234", saldo_centavos=1000)
        self.api = APIClient()
        self.api.force_authenticate(self.u)
        self.crv = Servico.objects.get(codigo="codigo-seguranca")  # R$ 30,00
        self.chamadas = []

    def falso_post(self, pago=True, amount=None):
        def post(url, json=None, headers=None, timeout=None):
            self.chamadas.append((url, json, headers))
            if url.endswith("/links"):
                return RespostaFalsa(200, {"url": "https://checkout.infinitepay.io/minha-loja/abc"})
            c = Cobranca.objects.get(id=json["order_nsu"])
            return RespostaFalsa(200, {"success": True, "paid": pago, "amount": amount or c.valor_centavos,
                                       "paid_amount": (amount or c.valor_centavos) + 10, "capture_method": "pix"})
        return post

    def pagar_consulta(self):
        with mock.patch("core.integracao.infinitepay.requests.post", side_effect=self.falso_post()):
            return self.api.post("/api/pagamentos/pedido/", {"servico": "codigo-seguranca", "placa": "abc1d23"}, format="json")

    def test_cobra_so_o_que_falta(self):
        r = self.pagar_consulta()
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.data["valor_centavos"], 2000)  # 30,00 - 10,00 de saldo
        self.assertTrue(r.data["url"].startswith("https://checkout.infinitepay.io"))
        url, corpo, cab = self.chamadas[0]
        self.assertEqual(corpo["items"][0]["price"], 2000)
        self.assertEqual(corpo["order_nsu"], Cobranca.objects.get().order_nsu)
        self.assertEqual(corpo["customer"]["phone_number"], "+5584999991234")
        self.assertIn("/pagamento/retorno?cobranca=", corpo["redirect_url"])
        self.assertEqual(corpo["webhook_url"], "https://api.teste/api/pagamentos/retorno/tokpg/")
        self.assertTrue(corpo["options"]["single_payment"])
        self.assertEqual(cab["X-Api-Key"], "inchk_teste")

    def test_valor_cheio_sem_saldo_e_minimo(self):
        self.u.saldo_centavos = 0
        self.u.save()
        self.assertEqual(self.pagar_consulta().data["valor_centavos"], 3000)
        self.u.saldo_centavos = 2950
        self.u.save()
        self.assertEqual(self.pagar_consulta().data["valor_centavos"], 100)  # mínimo R$ 1,00

    def test_com_saldo_suficiente_recusa(self):
        self.u.saldo_centavos = 5000
        self.u.save()
        self.assertEqual(self.pagar_consulta().status_code, 400)

    def test_confirmar_credita_e_faz_consulta_uma_vez(self):
        cid = self.pagar_consulta().data["id"]
        fonte = FonteFalsa(consulta={"resultado": {"encontrado": True, "dados": {"codigoSeguranca": "99887766"}}})
        with mock.patch("core.integracao.infinitepay.requests.post", side_effect=self.falso_post()), \
             mock.patch("core.servicos.ClienteFonte", return_value=fonte):
            r1 = self.api.post(f"/api/pagamentos/{cid}/", {"slug": "s1", "transaction_nsu": "t1"}, format="json")
            r2 = self.api.post(f"/api/pagamentos/{cid}/", {"slug": "s1", "transaction_nsu": "t1"}, format="json")
        self.assertEqual(r1.data["status"], "PAGA")
        self.assertIsNotNone(r1.data["pedido_id"])
        self.assertEqual(r2.data["pedido_id"], r1.data["pedido_id"])
        self.assertEqual(Pedido.objects.count(), 1)
        self.assertEqual(Pedido.objects.get().destaque, "99887766")
        self.u.refresh_from_db()
        self.assertEqual(self.u.saldo_centavos, 0)  # 10 + 20 pagos - 30 da consulta
        check = [c for c in self.chamadas if c[0].endswith("/payment_check")][0][1]
        self.assertEqual(check, {"order_nsu": Cobranca.objects.get().order_nsu, "transaction_nsu": "t1", "slug": "s1",
                                 "handle": "minha-loja"})

    def test_nao_pago_nao_credita(self):
        cid = self.pagar_consulta().data["id"]
        with mock.patch("core.integracao.infinitepay.requests.post", side_effect=self.falso_post(pago=False)):
            r = self.api.post(f"/api/pagamentos/{cid}/", {"slug": "s", "transaction_nsu": "t"}, format="json")
        self.assertEqual(r.data["status"], "PENDENTE")
        self.u.refresh_from_db()
        self.assertEqual(self.u.saldo_centavos, 1000)

    def test_webhook_confirma_no_provedor(self):
        with mock.patch("core.integracao.infinitepay.requests.post", side_effect=self.falso_post()):
            cid = self.api.post("/api/pagamentos/recarga/", {"valor_centavos": 5000}, format="json").data["id"]
        c = Cobranca.objects.get(pk=cid)
        corpo = {"order_nsu": c.order_nsu, "invoice_slug": "slugX", "transaction_nsu": "nsuX", "amount": 5000}
        # aviso forjado: provedor diz que não foi pago -> nada acontece
        with mock.patch("core.integracao.infinitepay.requests.post", side_effect=self.falso_post(pago=False)):
            self.assertEqual(self.client.post("/api/pagamentos/retorno/tokpg/", corpo, content_type="application/json").status_code, 200)
        self.u.refresh_from_db()
        self.assertEqual(self.u.saldo_centavos, 1000)
        with mock.patch("core.integracao.infinitepay.requests.post", side_effect=self.falso_post()):
            self.client.post("/api/pagamentos/retorno/tokpg/", corpo, content_type="application/json")
            self.client.post("/api/pagamentos/retorno/tokpg/", corpo, content_type="application/json")
        self.u.refresh_from_db()
        self.assertEqual(self.u.saldo_centavos, 6000)
        self.assertEqual(self.client.post("/api/pagamentos/retorno/errado/", corpo, content_type="application/json").status_code, 404)

    def test_webhook_pede_reenvio_se_nao_conseguir_confirmar(self):
        import requests as rq
        with mock.patch("core.integracao.infinitepay.requests.post", side_effect=self.falso_post()):
            cid = self.api.post("/api/pagamentos/recarga/", {"valor_centavos": 5000}, format="json").data["id"]
        c = Cobranca.objects.get(pk=cid)
        corpo = {"order_nsu": c.order_nsu, "invoice_slug": "sl", "transaction_nsu": "ns"}
        with mock.patch("core.integracao.infinitepay.requests.post", side_effect=rq.ConnectionError("fora do ar")):
            r = self.client.post("/api/pagamentos/retorno/tokpg/", corpo, content_type="application/json")
        self.assertEqual(r.status_code, 400)  # InfinitePay reenvia
        self.u.refresh_from_db()
        self.assertEqual(self.u.saldo_centavos, 1000)
        with mock.patch("core.integracao.infinitepay.requests.post", side_effect=self.falso_post()):
            r = self.client.post("/api/pagamentos/retorno/tokpg/", corpo, content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.u.refresh_from_db()
        self.assertEqual(self.u.saldo_centavos, 6000)
        self.assertEqual(Movimentacao.objects.filter(usuario=self.u, tipo="PAGAMENTO").count(), 1)

    def test_gestao_lista_pagamentos(self):
        with mock.patch("core.integracao.infinitepay.requests.post", side_effect=self.falso_post()):
            self.api.post("/api/pagamentos/recarga/", {"valor_centavos": 5000}, format="json")
        self.assertEqual(self.api.get("/api/admin/pagamentos/").status_code, 403)
        adm = APIClient()
        adm.force_authenticate(Usuario.objects.create_user("admx", password="x-senha-123", is_staff=True))
        lista = adm.get("/api/admin/pagamentos/").data
        self.assertEqual(lista[0]["usuario"], "Paula Gomes")
        self.assertEqual(lista[0]["status"], "PENDENTE")

    def test_recarga_confirmada_sem_servico_pode_ser_reconsultada(self):
        with mock.patch("core.integracao.infinitepay.requests.post", side_effect=self.falso_post()):
            cid = self.api.post("/api/pagamentos/recarga/", {"valor_centavos": 2000}, format="json").data["id"]
            for _ in range(2):
                r = self.api.post(f"/api/pagamentos/{cid}/", {"slug": "s", "transaction_nsu": "t"}, format="json")
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.data["status"], "PAGA")
                self.assertIsNone(r.data["pedido_id"])
        self.assertEqual(Movimentacao.objects.filter(usuario=self.u, tipo="PAGAMENTO").count(), 1)

    def test_amount_menor_credita_so_o_pago(self):
        with mock.patch("core.integracao.infinitepay.requests.post", side_effect=self.falso_post()):
            cid = self.api.post("/api/pagamentos/recarga/", {"valor_centavos": 5000}, format="json").data["id"]
        with mock.patch("core.integracao.infinitepay.requests.post", side_effect=self.falso_post(amount=1000)):
            self.api.post(f"/api/pagamentos/{cid}/", {"slug": "s", "transaction_nsu": "t"}, format="json")
        self.u.refresh_from_db()
        self.assertEqual(self.u.saldo_centavos, 2000)

    def test_pagamento_libera_bonus_de_indicacao(self):
        padrinho = Usuario.objects.create_user("pad", password="x-senha-123")
        self.u.indicado_por = padrinho
        self.u.save()
        with mock.patch("core.integracao.infinitepay.requests.post", side_effect=self.falso_post()):
            cid = self.api.post("/api/pagamentos/recarga/", {"valor_centavos": 2000}, format="json").data["id"]
            self.api.post(f"/api/pagamentos/{cid}/", {"slug": "s", "transaction_nsu": "t"}, format="json")
        padrinho.refresh_from_db()
        self.assertEqual(padrinho.saldo_centavos, 500)

    def test_outro_usuario_nao_ve_cobranca(self):
        with mock.patch("core.integracao.infinitepay.requests.post", side_effect=self.falso_post()):
            cid = self.api.post("/api/pagamentos/recarga/", {"valor_centavos": 2000}, format="json").data["id"]
        outro = APIClient()
        outro.force_authenticate(Usuario.objects.create_user("o2", password="x-senha-123"))
        self.assertEqual(outro.get(f"/api/pagamentos/{cid}/").status_code, 404)

    @override_settings(INFINITEPAY={**IP, "HANDLE": "", "API_KEY": ""})
    def test_desligado_sem_configuracao(self):
        self.assertEqual(self.pagar_consulta().status_code, 503)
        self.assertFalse(self.client.get("/api/config/").json()["checkout_ativo"])


# ---------------------------------------------------------------------------
# Armazenamento de arquivos
# ---------------------------------------------------------------------------
import os  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402


class ArmazenamentoTeste(TestCase):
    def test_nome_aleatorio_sem_nome_original(self):
        u = Usuario.objects.create_user("arq", password="x-senha-123")
        api = APIClient()
        api.force_authenticate(u)
        arq = SimpleUploadedFile("RG do Joao Silva.pdf", b"%PDF teste", content_type="application/pdf")
        r = api.post("/api/chat/conversas/", {"texto": "segue", "anexo": arq})
        self.assertEqual(r.status_code, 201, r.content)
        nome = Mensagem.objects.get().anexo.name
        self.assertRegex(nome, r"^chat/\d{4}/\d{2}/[0-9a-f]{32}\.pdf$")
        self.assertNotIn("Joao", nome)

    def test_download_autenticado_com_nome_original(self):
        u = Usuario.objects.create_user("arq2", password="x-senha-123")
        api = APIClient()
        api.force_authenticate(u)
        arq = SimpleUploadedFile("comprovante banco.png", b"\x89PNG", content_type="image/png")
        api.post("/api/chat/conversas/", {"texto": "segue", "anexo": arq})
        r = api.get(f"/api/chat/anexos/{Mensagem.objects.get().pk}/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("comprovante banco.png", r["Content-Disposition"])
        self.assertEqual(b"".join(r.streaming_content), b"\x89PNG")

    def _config_com(self, **extra):
        env = {**os.environ, "S3_BUCKET": "meu-bucket", "S3_ACCESS_KEY": "AKIA", "S3_SECRET_KEY": "x", **extra}
        codigo = ("import os,json,django;os.environ['DJANGO_SETTINGS_MODULE']='config.settings';django.setup();"
                  "from django.conf import settings as s;print(json.dumps(s.STORAGES['default']))")
        saida = subprocess.run([sys.executable, "-c", codigo], env=env, capture_output=True, text=True, check=True)
        return json.loads(saida.stdout.strip().splitlines()[-1])

    def test_config_s3_aws_privado_e_criptografado(self):
        cfg = self._config_com()
        self.assertEqual(cfg["BACKEND"], "storages.backends.s3.S3Storage")
        o = cfg["OPTIONS"]
        self.assertIsNone(o["default_acl"])
        self.assertFalse(o["file_overwrite"])
        self.assertTrue(o["querystring_auth"])
        self.assertEqual(o["region_name"], "sa-east-1")
        self.assertEqual(o["object_parameters"], {"ServerSideEncryption": "AES256"})

    def test_config_s3_compativel(self):
        o = self._config_com(S3_ENDPOINT="https://conta.r2.cloudflarestorage.com", S3_REGIAO="auto")["OPTIONS"]
        self.assertEqual(o["endpoint_url"], "https://conta.r2.cloudflarestorage.com")
        self.assertNotIn("object_parameters", o)


# ---------------------------------------------------------------------------
# Site público (sem login)
# ---------------------------------------------------------------------------
@override_settings(INFINITEPAY=IP, FRONTEND_URL="http://site.teste")
class SitePublicoTeste(TestCase):
    def setUp(self):
        cache.clear()
        self.crv = Servico.objects.get(codigo="codigo-seguranca")
        self.crv.preco_publico_centavos = 2500
        self.crv.publico_ativo = True
        self.crv.save()
        self.crlv = Servico.objects.get(codigo="crlv-rn")
        self.crlv.publico_ativo = False
        self.crlv.save()
        self.chamadas = []

    def post_falso(self, pago=True):
        def post(url, json=None, headers=None, timeout=None):
            self.chamadas.append((url, json))
            if url.endswith("/links"):
                return RespostaFalsa(200, {"url": "https://checkout.infinitepay.io/x"})
            c = Cobranca.objects.get(id=json["order_nsu"])
            return RespostaFalsa(200, {"success": True, "paid": pago, "amount": c.valor_centavos, "capture_method": "pix"})
        return post

    def dados(self, **extra):
        return {"servico": "codigo-seguranca", "placa": "abc-1d23", "nome": "Lucas Prado", "email": "Lucas@x.com",
                "telefone": "(84) 98888-7777", "aceite": True, **extra}

    def comprar(self, **extra):
        with mock.patch("core.integracao.infinitepay.requests.post", side_effect=self.post_falso()):
            return self.client.post("/api/publico/pagar/", self.dados(**extra), content_type="application/json")

    def confirmar(self, r, fonte):
        with mock.patch("core.integracao.infinitepay.requests.post", side_effect=self.post_falso()), \
             mock.patch("core.servicos.ClienteFonte", return_value=fonte), \
             self.captureOnCommitCallbacks(execute=True):
            return self.client.post(f"/api/publico/cobranca/{r.json()['cobranca']}/",
                                    {"t": r.json()["token"], "slug": "s", "transaction_nsu": "n"}, content_type="application/json")

    def test_servicos_com_preco_publico(self):
        lista = self.client.get("/api/publico/servicos/").json()
        self.assertEqual([s["codigo"] for s in lista], ["codigo-seguranca"])
        self.assertEqual(lista[0]["preco_centavos"], 2500)
        self.assertNotIn("veicular", json.dumps(lista))

    def test_compra_sem_login_cobra_preco_publico(self):
        r = self.comprar()
        self.assertEqual(r.status_code, 201, r.content)
        corpo = self.chamadas[0][1]
        self.assertEqual(corpo["items"][0]["price"], 2500)
        self.assertEqual(corpo["customer"]["email"], "lucas@x.com")
        self.assertIn("/c/retorno?cobranca=", corpo["redirect_url"])
        self.assertIn(r.json()["token"], corpo["redirect_url"])

    def test_validacoes_e_servico_inativo(self):
        r = self.comprar(nome="Lucas", telefone="12", aceite=False, placa="1")
        for campo in ("nome", "telefone", "aceite", "placa"):
            self.assertIn(campo, r.json())
        self.assertEqual(self.comprar(servico="crlv-rn").status_code, 400)

    def test_pagamento_confirmado_faz_consulta_e_envia_email(self):
        r = self.comprar()
        fonte = FonteFalsa(consulta={"resultado": {"encontrado": True, "dados": {"codigoSeguranca": "55554444"}}})
        c = self.confirmar(r, fonte).json()
        self.assertEqual(c["status"], "PAGA")
        token = c["pedido"]
        p = self.client.get(f"/api/publico/pedido/{token}/").json()
        self.assertEqual(p["status"], "SUCESSO")
        self.assertEqual(p["codigo_seguranca"], "55554444")
        self.assertEqual(Pedido.objects.get().usuario, None)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(f"/c/{token}", mail.outbox[0].body)
        self.assertEqual(mail.outbox[0].to, ["lucas@x.com"])

    def test_historico_da_placa_nao_mostra_resultado(self):
        r = self.comprar()
        fonte = FonteFalsa(consulta={"resultado": {"encontrado": True, "dados": {"codigoSeguranca": "55554444"}}})
        token = self.confirmar(r, fonte).json()["pedido"]
        h = self.client.get("/api/publico/placa/ABC1D23/").json()
        self.assertEqual(h["consultas"][0]["status"], "SUCESSO")
        texto = json.dumps(h)
        for segredo in ("55554444", token, "lucas@x.com", "Lucas"):
            self.assertNotIn(segredo, texto)

    def test_token_errado_nao_abre(self):
        r = self.comprar()
        cid = r.json()["cobranca"]
        self.assertEqual(self.client.post(f"/api/publico/cobranca/{cid}/", {"t": "errado"}, content_type="application/json").status_code, 404)
        self.assertEqual(self.client.get("/api/publico/pedido/" + "x" * 43 + "/").status_code, 404)

    def test_falha_marca_reembolso_e_gestao_resolve(self):
        r = self.comprar()
        token = self.confirmar(r, FonteFalsa(erro=ErroFonte("x", status_http=422))).json()["pedido"]
        p = self.client.get(f"/api/publico/pedido/{token}/").json()
        self.assertEqual(p["status"], "FALHA")
        self.assertTrue(p["reembolso_pendente"])
        self.assertIn("estornado", p["mensagem"])
        self.assertIn("Não foi possível concluir", mail.outbox[-1].subject)
        adm = APIClient()
        adm.force_authenticate(Usuario.objects.create_user("gest", password="x-senha-123", is_staff=True))
        lista = adm.get("/api/admin/pedidos/?reembolso=1").json()["results"]
        self.assertEqual(lista[0]["usuario"], "Lucas Prado (site)")
        pid = Pedido.objects.get().pk
        self.assertEqual(adm.post(f"/api/admin/pedidos/{pid}/reembolsado/").status_code, 200)
        self.assertTrue(self.client.get(f"/api/publico/pedido/{token}/").json()["reembolsado"])

    def test_reenviar_links_so_para_email_da_compra(self):
        r = self.comprar()
        self.confirmar(r, FonteFalsa(consulta={"resultado": {"encontrado": True, "dados": {}}}))
        mail.outbox.clear()
        r1 = self.client.post("/api/publico/placa/ABC1D23/links/", {"email": "outro@x.com"}, content_type="application/json")
        self.assertEqual(len(mail.outbox), 0)
        r2 = self.client.post("/api/publico/placa/ABC1D23/links/", {"email": "LUCAS@x.com"}, content_type="application/json")
        self.assertEqual(r1.json(), r2.json())
        self.assertEqual(len(mail.outbox), 1)

    def test_chat_de_visitante(self):
        r = self.client.post("/api/publico/chat/", {"nome": "Ana", "contato": "84999990000", "texto": "Oi, dúvida", "placa": "abc1d23"},
                             content_type="application/json")
        self.assertEqual(r.status_code, 201, r.content)
        tok = r.json()["token"]
        adm = APIClient()
        adm.force_authenticate(Usuario.objects.create_user("eqp", password="x-senha-123", is_staff=True, first_name="Rita"))
        conv = adm.get("/api/chat/conversas/").json()[0]
        self.assertTrue(conv["cliente"]["visitante"])
        self.assertEqual(conv["assunto"], "Site · ABC1D23")
        adm.post(f"/api/chat/conversas/{conv['id']}/mensagens/", {"texto": "Olá Ana!"}, format="json")
        self.assertEqual(self.client.get(f"/api/publico/chat/{tok}/nao-lidas/").json()["total"], 1)
        msgs = self.client.get(f"/api/publico/chat/{tok}/mensagens/").json()["mensagens"]
        self.assertEqual([m["autor"] for m in msgs], ["Você", "Equipe · Rita"])
        self.assertEqual(self.client.get(f"/api/publico/chat/{tok}/nao-lidas/").json()["total"], 0)
        self.assertEqual(self.client.get("/api/publico/chat/" + "y" * 43 + "/mensagens/").status_code, 404)
        # cliente logado não enxerga conversa de visitante
        cli = APIClient()
        cli.force_authenticate(Usuario.objects.create_user("cl9", password="x-senha-123"))
        self.assertEqual(cli.get("/api/chat/conversas/").json(), [])

    def test_gestao_configura_preco_publico(self):
        adm = APIClient()
        adm.force_authenticate(Usuario.objects.create_user("g2", password="x-senha-123", is_staff=True))
        r = adm.patch(f"/api/admin/servicos/{self.crlv.pk}/", {"preco_publico_centavos": 6990, "publico_ativo": True}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(adm.patch(f"/api/admin/servicos/{self.crlv.pk}/", {"preco_publico_centavos": 50}, format="json").status_code, 400)
        precos = {s["codigo"]: s["preco_centavos"] for s in self.client.get("/api/publico/servicos/").json()}
        self.assertEqual(precos["crlv-rn"], 6990)

    def test_gestao_lista_e_busca_pagamentos_publicos(self):
        self.comprar()
        adm = APIClient()
        adm.force_authenticate(Usuario.objects.create_user("gestpg", password="x-senha-123", is_staff=True))
        for busca in ("", "Lucas", "lucas@x.com", "ABC1D23"):
            with self.subTest(busca=busca):
                r = adm.get("/api/admin/pagamentos/", {"busca": busca, "status": "PENDENTE"})
                self.assertEqual(r.status_code, 200)
                self.assertEqual(len(r.data), 1)
                self.assertEqual(r.data[0]["usuario"], "Lucas Prado (site)")
                self.assertEqual(r.data[0]["tipo"], "PUBLICO")
        self.assertEqual(adm.get("/api/admin/pagamentos/", {"status": "PAGA", "busca": "Lucas"}).data, [])
        Cobranca.objects.update(comprador_nome="")
        self.assertEqual(adm.get("/api/admin/pagamentos/").data[0]["usuario"], "Visitante (site)")
