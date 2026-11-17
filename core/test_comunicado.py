import base64
from unittest import mock

import requests
from django.core.cache import cache
from django.test import TestCase, SimpleTestCase, override_settings
from rest_framework.test import APIClient

from .comunicado import ComunicadoVendaSerializer, montar_payload
from .integracao.carven import ClienteComunicado, ErroComunicado, situacao
from .models import Cobranca, Movimentacao, Pedido, Servico, Usuario
from . import servicos

CFG = {"URL": "https://homologacao.carven.com.br/api", "CLIENT_ID": "teste", "CLIENT_SECRET": "segredo",
       "TIMEOUT": 5, "WEBHOOK_BASE": "https://backend.example.com", "WEBHOOK_TOKEN": "token-teste"}


def formulario():
    return {"vendedor": {"tipo_pessoa": "F", "documento": "52998224725", "nome": "Maria Silva"},
            "comprador": {"tipo_pessoa": "F", "documento": "11144477735", "nome": "Joao Silva"},
            "endereco_comprador": {"cep": "59000000", "logradouro": "Rua A", "numero": "1", "bairro": "Centro", "cidade_ibge": "2408102"},
            "venda_data": "2026-01-10", "venda_valor": "10000.00", "renavam": "00123456797",
            "ano_fabricacao": 2020, "ano_modelo": 2021, "crv_numero": "123456789", "crv_codigo_seguranca": "12345678901",
            "crv_via": 1, "crv_data_emissao": "2020-01-01", "crv_uf": "RN"}


@override_settings(CARVEN=CFG)
class ComunicadoFluxoTeste(TestCase):
    def setUp(self):
        cache.clear()
        self.usuario = Usuario.objects.create_user("comunicado", saldo_centavos=10000)
        self.api = APIClient()
        self.api.force_authenticate(self.usuario)
        self.servico = Servico.objects.get(codigo="comunicado-venda")
        self.servico.ativo = self.servico.publico_ativo = True
        self.servico.preco_centavos = self.servico.preco_publico_centavos = 2000
        self.servico.save()
        self.fonte = mock.Mock()
        self.fonte.inserir.return_value = ("PROTO-1", False)
        self.fonte.detalhes.return_value = {"situacao": {"codigo": 2}}
        self.fonte.comprovante.return_value = b"%PDF-1.4 teste"
        self.patch = mock.patch("core.integracao.carven.ClienteComunicado", return_value=self.fonte)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def pedir(self, dados=None):
        return self.api.post("/api/pedidos/", {"servico": self.servico.codigo, "placa": "ABC1D23", "dados": dados or formulario()}, format="json")

    def test_fluxo_completo_preserva_pdf_e_remove_segredos(self):
        r = self.pedir()
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["status"], "SUCESSO")
        p = Pedido.objects.get()
        self.assertTrue(p.documento)
        self.assertNotIn("crv_codigo_seguranca", p.dados_entrada)
        self.assertNotIn("arquivos", p.dados_entrada)
        payload = self.fonte.inserir.call_args.args[0]
        self.assertEqual(payload["venda"]["data"], "10/01/2026")
        self.assertEqual(payload["comprador"]["endereco"]["cidade"], 2408102)
        self.assertIn(str(p.pk), payload["url_notificacao"])
        self.assertNotIn("dados_entrada", r.data)
        self.usuario.refresh_from_db()
        self.assertEqual(self.usuario.saldo_centavos, 8000)

    def test_sem_formulario_nao_debita(self):
        r = self.api.post("/api/pedidos/", {"servico": self.servico.codigo, "placa": "ABC1D23"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertFalse(Pedido.objects.exists())
        self.assertFalse(Movimentacao.objects.exists())
        self.fonte.inserir.assert_not_called()

    def test_cpf_invalido_nao_debita(self):
        dados = formulario(); dados["comprador"]["documento"] = "11111111111"
        self.assertEqual(self.pedir(dados).status_code, 400)
        self.assertFalse(Pedido.objects.exists())

    def test_bloqueada_espera_liberacao_sem_reinserir(self):
        self.fonte.inserir.return_value = ("PROTO-1", True)
        self.assertEqual(self.pedir().status_code, 202)
        self.fonte.transmitir.assert_not_called()
        self.fonte.detalhes.return_value = {"situacao": {"codigo": 1}}
        with mock.patch("core.servicos._verificar_comunicado", wraps=servicos._verificar_comunicado):
            # Após transmissão, consulta final indica sucesso.
            self.fonte.detalhes.side_effect = [{"situacao": {"codigo": 1}}, {"situacao": {"codigo": 2}}]
            servicos.sincronizar_pedido(Pedido.objects.get(), forcar=True)
        self.assertEqual(self.fonte.inserir.call_count, 1)
        self.fonte.transmitir.assert_called_once_with("PROTO-1")
        self.assertEqual(Pedido.objects.get().status, "SUCESSO")

    def test_timeout_insercao_nao_repete_nem_estorna(self):
        self.fonte.inserir.side_effect = ErroComunicado("timeout", rede=True)
        self.assertEqual(self.pedir().status_code, 202)
        p = Pedido.objects.get()
        self.assertTrue(p.envio_incerto)
        servicos.sincronizar_pedido(p, forcar=True)
        self.assertEqual(self.fonte.inserir.call_count, 1)
        self.assertFalse(Pedido.objects.get().estornado)

    def test_login_temporariamente_indisponivel_nao_marca_insercao_incerta(self):
        self.fonte.inserir.side_effect = ErroComunicado("Falha no login", rede=True, nao_enviado=True)
        self.assertEqual(self.pedir().status_code, 202)
        p = Pedido.objects.get()
        self.assertFalse(p.envio_incerto)
        self.assertFalse(p.estornado)
        servicos.sincronizar_pedido(p, forcar=True)
        self.assertEqual(self.fonte.inserir.call_count, 2)

    def test_login_falha_repetida_estorna_sem_cadastro_externo(self):
        self.fonte.inserir.side_effect = ErroComunicado("Falha no login", rede=True, nao_enviado=True)
        self.pedir()
        for _ in range(servicos.MAX_TENTATIVAS_ENVIO):
            servicos.sincronizar_pedido(Pedido.objects.get(), forcar=True)
        p = Pedido.objects.get()
        self.assertEqual(p.status, "FALHA")
        self.assertTrue(p.estornado)
        self.assertNotIn("crv_codigo_seguranca", p.dados_entrada)
        self.fonte.transmitir.assert_not_called()

    def test_timeout_transmissao_reconsulta_antes_de_reenviar(self):
        self.fonte.transmitir.side_effect = ErroComunicado("timeout", rede=True)
        self.assertEqual(self.pedir().status_code, 202)
        servicos.sincronizar_pedido(Pedido.objects.get(), forcar=True)
        self.assertEqual(self.fonte.inserir.call_count, 1)
        self.assertEqual(self.fonte.transmitir.call_count, 1)
        self.assertEqual(Pedido.objects.get().status, "SUCESSO")

    def test_erro_raiz_estorna_uma_vez_e_limpa_dados(self):
        self.fonte.detalhes.return_value = {"situacao": {"codigo": 1}, "ultimo_erro": {"descricacao": "Documento recusado"}}
        r = self.pedir()
        self.assertEqual(r.data["status"], "FALHA")
        self.assertIn("Documento recusado", r.data["mensagem"])
        p = Pedido.objects.get()
        self.assertNotIn("crv_codigo_seguranca", p.dados_entrada)
        servicos.sincronizar_pedido(p, forcar=True)
        self.assertEqual(Movimentacao.objects.filter(tipo="ESTORNO").count(), 1)

    def test_pdf_indisponivel_mantem_processamento(self):
        self.fonte.comprovante.side_effect = ErroComunicado("PDF indisponível", status_http=404)
        self.assertEqual(self.pedir().data["status"], "PROCESSANDO")
        self.assertFalse(Pedido.objects.get().estornado)

    def test_catalogo_indica_formulario(self):
        for rota in ("/api/servicos/", "/api/publico/servicos/"):
            itens = self.api.get(rota).data
            s = next(s for s in itens if s["codigo"] == self.servico.codigo)
            self.assertEqual(s["formulario"], "COMUNICADO_VENDA")
            self.assertNotIn("fonte", s)

    @override_settings(INFINITEPAY={"HANDLE": "teste", "API_KEY": "", "WEBHOOK_BASE": "", "WEBHOOK_TOKEN": "teste", "VALIDADE_HORAS": 2})
    def test_checkout_painel_e_publico_guardam_formulario_validado(self):
        with mock.patch("core.integracao.infinitepay.configurado", return_value=True), mock.patch("core.servicos._gerar_link", side_effect=lambda c, d: c):
            self.usuario.saldo_centavos = 0; self.usuario.save()
            for rota, extra in [("/api/pagamentos/pedido/", {}), ("/api/publico/pagar/", {"nome": "Joao Silva", "email": "joao@example.com", "telefone": "84999990000", "aceite": True})]:
                r = self.api.post(rota, {"servico": self.servico.codigo, "placa": "ABC1D23", "dados": formulario(), **extra}, format="json")
                self.assertEqual(r.status_code, 201, r.data)
            self.assertEqual(Cobranca.objects.count(), 2)
            for c in Cobranca.objects.all(): self.assertEqual(c.dados_entrada["venda_data"], "10/01/2026")

    def test_webhook_nao_confia_no_corpo_e_exige_token(self):
        self.fonte.detalhes.return_value = {"situacao": {"codigo": 1}}
        self.pedir()
        p = Pedido.objects.get()
        self.assertEqual(self.api.post(f"/api/comunicado/retorno/errado/{p.pk}/", {}, format="json").status_code, 404)
        Pedido.objects.filter(pk=p.pk).update(ultima_verificacao=None)
        self.fonte.detalhes.return_value = {"situacao": {"codigo": 2}}
        with self.captureOnCommitCallbacks(execute=True):
            r = self.api.post(f"/api/comunicado/retorno/token-teste/{p.pk}/", {"situacao": {"codigo": 5}}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Pedido.objects.get().status, "SUCESSO")

    def test_pagamento_publico_emite_uma_vez_e_libera_pdf_por_token(self):
        ser = ComunicadoVendaSerializer(data=formulario())
        ser.is_valid(raise_exception=True)
        c = Cobranca.objects.create(tipo=Cobranca.Tipo.PUBLICO, status=Cobranca.Status.PAGA,
            servico=self.servico, placa="ABC1D23", valor_centavos=2000, token_acesso="acesso-secreto-123456789012345",
            comprador_email="joao@example.com", dados_entrada=ser.validated_data)
        with mock.patch("core.servicos.notificar_publico"):
            servicos._emitir_pedido_pago(c.pk)
            servicos._emitir_pedido_pago(c.pk)
        self.assertEqual(Pedido.objects.count(), 1)
        self.assertEqual(self.fonte.inserir.call_count, 1)
        c.refresh_from_db()
        self.assertIsNone(c.dados_entrada)
        visitante = APIClient()
        self.assertEqual(visitante.get(f"/api/publico/pedido/{c.token_acesso}/").data["status"], "SUCESSO")
        resposta = visitante.get(f"/api/publico/pedido/{c.token_acesso}/documento/")
        self.assertEqual(resposta.status_code, 200)
        self.assertTrue(b"".join(resposta.streaming_content).startswith(b"%PDF-"))
        self.assertEqual(visitante.get("/api/publico/pedido/token-incorreto-1234567890/documento/").status_code, 404)

    def test_reconciliar_valida_identidade_e_nao_transmite(self):
        import io
        from django.core.management import call_command
        from django.core.management.base import CommandError
        self.fonte.inserir.side_effect = ErroComunicado("timeout", rede=True)
        self.pedir()
        p = Pedido.objects.get()
        self.fonte.detalhes.return_value = {"veiculo": {"placa": "OUT1234"}}
        with self.assertRaises(CommandError):
            call_command("reconciliar_comunicado", str(p.pk), "PROTO-1", stdout=io.StringIO())
        self.fonte.detalhes.return_value = {"veiculo": {"placa": "ABC1D23", "renavam": "00123456797"},
            "vendedor": {"cpf": "52998224725"}, "comprador": {"cpf": "11144477735"}}
        call_command("reconciliar_comunicado", str(p.pk), "PROTO-1", stdout=io.StringIO())
        p.refresh_from_db()
        self.assertFalse(p.envio_incerto)
        self.assertEqual(p.protocolo_fonte, "PROTO-1")
        self.fonte.transmitir.assert_not_called()


@override_settings(CARVEN=CFG)
class ClienteComunicadoTeste(SimpleTestCase):
    def setUp(self): cache.clear()

    def resposta(self, status=200, dados=None, conteudo=b""):
        r = mock.Mock(status_code=status, content=conteudo)
        r.json.return_value = dados if dados is not None else {"status": "sucesso"}
        return r

    @mock.patch("core.integracao.carven.requests.post")
    @mock.patch("core.integracao.carven.requests.request")
    def test_401_renova_token_uma_vez(self, req, login):
        login.side_effect = [self.resposta(dados={"access_token": "a"}), self.resposta(dados={"access_token": "b"})]
        req.side_effect = [self.resposta(401), self.resposta(dados={"situacao": {"codigo": 2}})]
        self.assertEqual(ClienteComunicado().detalhes("P")["situacao"]["codigo"], 2)
        self.assertEqual(login.call_count, 2)
        self.assertEqual(req.call_args.kwargs["headers"]["Authorization"], "Bearer b")

    @mock.patch.object(ClienteComunicado, "_token", return_value="t")
    @mock.patch("core.integracao.carven.requests.request")
    def test_rejeita_json_em_lugar_de_pdf(self, req, token):
        req.return_value = self.resposta(conteudo=b'{"status":"erro"}')
        with self.assertRaises(ErroComunicado): ClienteComunicado().comprovante("P")

    @mock.patch.object(ClienteComunicado, "_token", return_value="t")
    @mock.patch("core.integracao.carven.requests.request")
    def test_erro_de_rede_tipado(self, req, token):
        req.side_effect = requests.Timeout()
        with self.assertRaises(ErroComunicado) as erro: ClienteComunicado().inserir({})
        self.assertTrue(erro.exception.temporario)

    def test_ultimo_erro_documentado_na_raiz(self):
        self.assertEqual(situacao({"situacao": {"codigo": 8}, "ultimo_erro": {"descricacao": "Limite excedido"}}), (8, "Limite excedido"))

    def test_payload_anexos_e_validacao_base64(self):
        dados = formulario()
        dados["arquivos"] = [{"nome": "dut.pdf", "mime_type": "application/pdf", "base64": base64.b64encode(b"%PDF-1.4").decode()}]
        ser = ComunicadoVendaSerializer(data=dados)
        self.assertTrue(ser.is_valid(), ser.errors)
        anexo = montar_payload(ser.validated_data, "ABC1D23")["arquivos"][0]
        self.assertEqual(anexo["tamanho"], 8)
        self.assertEqual(len(anexo["hash"]), 32)
        dados["arquivos"][0]["base64"] = "%%%"
        self.assertFalse(ComunicadoVendaSerializer(data=dados).is_valid())

    @mock.patch("core.integracao.carven.requests.post")
    def test_login_redirecionado_nao_segue_waf_nem_marca_envio_incerto(self, login):
        login.return_value = self.resposta(status=302)
        with self.assertRaises(ErroComunicado) as erro:
            ClienteComunicado()._token()
        self.assertTrue(erro.exception.nao_enviado)
        self.assertTrue(erro.exception.temporario)
        self.assertIs(login.call_args.kwargs["allow_redirects"], False)

    @mock.patch.object(ClienteComunicado, "_token", return_value="t")
    @mock.patch("core.integracao.carven.requests.request")
    def test_insercao_redirecionada_nao_segue_waf(self, req, token):
        req.return_value = self.resposta(status=302)
        with self.assertRaises(ErroComunicado):
            ClienteComunicado().inserir({})
        self.assertIs(req.call_args.kwargs["allow_redirects"], False)

    @mock.patch("core.integracao.carven.requests.post")
    def test_timeout_login_ocorre_antes_de_enviar_comunicado(self, login):
        login.side_effect = requests.Timeout()
        with self.assertRaises(ErroComunicado) as erro:
            ClienteComunicado().inserir({})
        self.assertTrue(erro.exception.nao_enviado)


class CepComunicadoTeste(TestCase):
    @mock.patch("core.comunicado.requests.get")
    def test_resposta_inesperada_cep_retorna_503(self, req):
        cache.clear()
        req.return_value = mock.Mock(status_code=200)
        req.return_value.json.return_value = []
        self.assertEqual(APIClient().get("/api/cep/59000000/").status_code, 503)
