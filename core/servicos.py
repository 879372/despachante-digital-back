"""Regras de negócio: carteira, pedidos e sincronização com a fonte."""

import base64
import binascii
import logging
import re
import unicodedata
import uuid
from datetime import timedelta

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from .integracao.cliente import ClienteFonte, ErroFonte, higienizar
from .models import Cobranca, Movimentacao, Pedido, Usuario

log = logging.getLogger("core.pedidos")

PLACA_RE = re.compile(r"^[A-Z]{3}[0-9][A-Z0-9][0-9]{2}$")
MAX_TENTATIVAS_ENVIO = 8


class SaldoInsuficiente(Exception):
    pass


def normalizar_placa(placa: str) -> str:
    placa = re.sub(r"[^A-Za-z0-9]", "", placa or "").upper()
    if not PLACA_RE.match(placa):
        raise ValueError("Placa inválida. Use o formato ABC1234 ou ABC1D23.")
    return placa


# --- Carteira ---------------------------------------------------------------

def _movimentar(usuario_bloqueado, tipo, valor, descricao, pedido=None):
    usuario_bloqueado.saldo_centavos += valor
    usuario_bloqueado.save(update_fields=["saldo_centavos"])
    return Movimentacao.objects.create(
        usuario=usuario_bloqueado,
        tipo=tipo,
        valor_centavos=valor,
        saldo_apos_centavos=usuario_bloqueado.saldo_centavos,
        descricao=descricao,
        pedido=pedido,
    )


@transaction.atomic
def ajustar_saldo(usuario_id, valor, descricao, tipo=Movimentacao.Tipo.AJUSTE):
    usuario = Usuario.objects.select_for_update().get(pk=usuario_id)
    if usuario.saldo_centavos + valor < 0:
        raise SaldoInsuficiente("O ajuste deixaria o saldo negativo.")
    return _movimentar(usuario, tipo, valor, descricao)


# --- Indicação ----------------------------------------------------------------

def pagar_bonus_indicacao(indicado_id):
    """Credita o bônus para quem indicou e para o indicado. Executa uma única vez."""
    valor = settings.BONUS_INDICACAO_CENTAVOS
    with transaction.atomic():
        indicado = Usuario.objects.select_for_update().get(pk=indicado_id)
        if indicado.bonus_indicacao_pago or not indicado.indicado_por_id or valor <= 0:
            return False
        padrinho = Usuario.objects.select_for_update().get(pk=indicado.indicado_por_id)
        if not padrinho.is_active:
            return False
        nome = (indicado.first_name or indicado.username).split(" ")[0]
        _movimentar(padrinho, Movimentacao.Tipo.BONUS, valor, f"Bônus: indicação de {nome}")
        indicado.bonus_indicacao_pago = True
        indicado.save(update_fields=["bonus_indicacao_pago"])
        _movimentar(indicado, Movimentacao.Tipo.BONUS, valor, "Bônus de boas-vindas (indicação)")
    return True


# --- Pedidos ----------------------------------------------------------------

def criar_pedido(usuario, servico, placa, cliente=None):
    placa = normalizar_placa(placa)
    with transaction.atomic():
        u = Usuario.objects.select_for_update().get(pk=usuario.pk)
        if u.saldo_centavos < servico.preco_centavos:
            raise SaldoInsuficiente("Saldo insuficiente. Adicione saldo para continuar.")
        pedido = Pedido.objects.create(
            usuario=u,
            servico=servico,
            placa=placa,
            valor_centavos=servico.preco_centavos,
            chave_idempotencia=f"ped-{uuid.uuid4().hex}",
            mensagem="Solicitação recebida.",
        )
        _movimentar(u, Movimentacao.Tipo.DEBITO, -servico.preco_centavos, f"{servico.nome} · {placa}", pedido)

    enviar_pedido(pedido, cliente)
    pedido.refresh_from_db()
    return pedido


def enviar_pedido(pedido, cliente=None):
    """Envia (ou reenvia, com a mesma chave de idempotência) para a fonte."""
    cliente = cliente or ClienteFonte()
    servico = pedido.servico
    Pedido.objects.filter(pk=pedido.pk).update(tentativas_envio=pedido.tentativas_envio + 1)
    try:
        if servico.assincrono:
            codigo, dados = cliente.solicitar_emissao(servico.sku_fonte, pedido.placa, pedido.chave_idempotencia)
        else:
            codigo, dados = cliente.consultar(servico.sku_fonte, pedido.placa, pedido.chave_idempotencia)
    except ErroFonte as erro:
        pedido.refresh_from_db()
        if erro.temporario and pedido.tentativas_envio < MAX_TENTATIVAS_ENVIO:
            Pedido.objects.filter(pk=pedido.pk).update(mensagem="Aguardando processamento. Tentaremos novamente.")
            return
        _finalizar_com_falha(pedido.pk, erro.mensagem_publica, cobrado=erro.cobrado)
        return

    aplicar_retorno(pedido.pk, dados, enviado=True)


def _status_fonte(dados):
    status = str(dados.get("status") or "").upper()
    if status in ("PENDENTE", "PROCESSANDO", "SUCESSO", "FALHA"):
        return status
    if status in ("SEM_RESULTADO", "NAO_ENCONTRADO"):
        return "SEM_RESULTADO"
    resultado = dados.get("resultado")
    if isinstance(resultado, dict):
        return "SUCESSO" if resultado.get("encontrado", True) else "SEM_RESULTADO"
    return "PROCESSANDO" if dados.get("consultaId") else ""


def _normalizar_chave(chave):
    texto = unicodedata.normalize("NFKD", str(chave)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", texto)


def extrair_codigo_seguranca(dados):
    """Procura recursivamente um campo de código de segurança."""
    if isinstance(dados, dict):
        for chave, valor in dados.items():
            k = _normalizar_chave(chave)
            if "seguranca" in k and isinstance(valor, (str, int)) and str(valor).strip():
                return str(valor).strip()
        for valor in dados.values():
            achado = extrair_codigo_seguranca(valor)
            if achado:
                return achado
    elif isinstance(dados, list):
        for item in dados:
            achado = extrair_codigo_seguranca(item)
            if achado:
                return achado
    return ""


def aplicar_retorno(pedido_id, dados, enviado=False):
    """Atualiza o pedido a partir de um retorno da fonte (envio, status ou webhook)."""
    dados = dados if isinstance(dados, dict) else {}
    status = _status_fonte(dados)

    with transaction.atomic():
        pedido = Pedido.objects.select_for_update().select_related("servico").get(pk=pedido_id)
        if not pedido.aberto:
            return pedido  # já finalizado: avisos atrasados não sobrescrevem
        pedido.ultima_verificacao = timezone.now()
        if enviado:
            pedido.enviado = True
        if dados.get("consultaId") and not pedido.protocolo_fonte:
            pedido.protocolo_fonte = str(dados["consultaId"])[:80]
        if "cobrado" in dados:
            pedido.cobrado_fonte = bool(dados.get("cobrado"))
        if isinstance(dados.get("valorCobradoCentavos"), int):
            pedido.custo_centavos = dados["valorCobradoCentavos"]

        if status in ("PENDENTE", "PROCESSANDO", ""):
            pedido.status = Pedido.Status.PROCESSANDO if status == "PROCESSANDO" else Pedido.Status.PENDENTE
            if pedido.servico.assincrono:
                pedido.mensagem = "Emissão em andamento junto ao órgão. Prazo estimado: 1 a 3 dias úteis."
            else:
                pedido.mensagem = "Em processamento."
            pedido.save()
            return pedido

        if status == "SUCESSO":
            resultado = dados.get("resultado") or {}
            documento = resultado.pop("documento", None) if isinstance(resultado, dict) else None
            if isinstance(documento, dict) and documento.get("base64"):
                try:
                    conteudo = base64.b64decode(documento["base64"], validate=True)
                    ext = str(documento.get("formato") or "pdf").lower().strip(".")[:5]
                    nome = f"{pedido.servico.codigo}-{pedido.placa}-{pedido.pk.hex[:8]}.{ext}"
                    pedido.documento.save(nome, ContentFile(conteudo), save=False)
                except (binascii.Error, ValueError):
                    log.error("Pedido %s: documento base64 inválido", pedido.pk)
            limpo = higienizar(resultado)
            pedido.resultado = limpo
            pedido.destaque = extrair_codigo_seguranca(limpo)[:120]
            pedido.status = Pedido.Status.SUCESSO
            pedido.mensagem = "Concluído."
            pedido.save()
            if pedido.origem == Pedido.Origem.PUBLICO:
                transaction.on_commit(lambda: notificar_publico(pedido.pk))
            return pedido

    # FALHA ou SEM_RESULTADO
    mensagem = (
        "Nenhum registro encontrado para esta placa."
        if status == "SEM_RESULTADO"
        else "Não foi possível concluir a emissão."
    )
    return _finalizar_com_falha(pedido_id, mensagem, status=status, cobrado=bool(dados.get("cobrado")))


def _finalizar_com_falha(pedido_id, mensagem, status="FALHA", cobrado=False):
    with transaction.atomic():
        pedido = Pedido.objects.select_for_update().get(pk=pedido_id)
        if not pedido.aberto:
            return pedido
        pedido.status = Pedido.Status.SEM_RESULTADO if status == "SEM_RESULTADO" else Pedido.Status.FALHA
        pedido.cobrado_fonte = cobrado
        pedido.mensagem = mensagem
        if pedido.origem == Pedido.Origem.PUBLICO:
            pedido.reembolso_pendente = True
            pedido.mensagem = f"{mensagem} O valor pago será estornado para a sua forma de pagamento."
            pedido.save()
            transaction.on_commit(lambda: notificar_publico(pedido.pk))
            return pedido
        if settings.ESTORNAR_FALHAS and not pedido.estornado:
            u = Usuario.objects.select_for_update().get(pk=pedido.usuario_id)
            _movimentar(u, Movimentacao.Tipo.ESTORNO, pedido.valor_centavos, f"Estorno · {pedido.placa}", pedido)
            pedido.estornado = True
            pedido.mensagem = f"{mensagem} O valor foi devolvido ao seu saldo."
        pedido.save()
        return pedido


def sincronizar_pedido(pedido, cliente=None, forcar=False):
    """Consulta o andamento de um pedido aberto. Retorna o pedido atualizado."""
    if not pedido.aberto:
        return pedido
    if not forcar and pedido.ultima_verificacao and timezone.now() - pedido.ultima_verificacao < timedelta(minutes=1):
        return pedido
    cliente = cliente or ClienteFonte()

    if not pedido.protocolo_fonte:
        if not pedido.enviado:
            enviar_pedido(pedido, cliente)
        pedido.refresh_from_db()
        return pedido

    try:
        _, dados = cliente.status(pedido.protocolo_fonte)
    except ErroFonte as erro:
        log.warning("Pedido %s: falha ao consultar status: %s", pedido.pk, erro.interno)
        Pedido.objects.filter(pk=pedido.pk).update(ultima_verificacao=timezone.now())
        pedido.refresh_from_db()
        return pedido
    return aplicar_retorno(pedido.pk, dados)


def sincronizar_abertos(cliente=None, limite=200):
    cliente = cliente or ClienteFonte()
    total = 0
    for pedido in Pedido.objects.filter(status__in=Pedido.ABERTOS).select_related("servico")[:limite]:
        sincronizar_pedido(pedido, cliente, forcar=True)
        total += 1
    return total


# --- Pagamento online (checkout) -------------------------------------------

def valor_a_pagar(usuario, servico):
    """Quanto falta para pagar o serviço. Respeita o mínimo do checkout (R$ 1,00)."""
    from .integracao.infinitepay import MINIMO_CENTAVOS

    falta = servico.preco_centavos - usuario.saldo_centavos
    return 0 if falta <= 0 else max(falta, MINIMO_CENTAVOS)


def iniciar_checkout_pedido(usuario, servico, placa):
    from .models import Cobranca

    placa = normalizar_placa(placa)
    usuario = Usuario.objects.get(pk=usuario.pk)
    valor = valor_a_pagar(usuario, servico)
    if valor == 0:
        raise ValueError("Você já tem saldo suficiente para esta consulta.")
    cobranca = Cobranca.objects.create(usuario=usuario, tipo=Cobranca.Tipo.PEDIDO, valor_centavos=valor,
                                       servico=servico, placa=placa)
    descricao = f"{servico.nome} - placa {placa}" if valor >= servico.preco_centavos else f"{servico.nome} - placa {placa} (complemento)"
    return _gerar_link(cobranca, descricao)


def iniciar_checkout_recarga(usuario, valor_centavos):
    from .models import Cobranca

    if valor_centavos < 1000:
        raise ValueError("Valor mínimo de recarga: R$ 10,00.")
    if valor_centavos > 500000:
        raise ValueError("Valor máximo por recarga: R$ 5.000,00.")
    cobranca = Cobranca.objects.create(usuario=usuario, tipo=Cobranca.Tipo.RECARGA, valor_centavos=valor_centavos)
    return _gerar_link(cobranca, "Recarga de saldo")


def _gerar_link(cobranca, descricao):
    from .integracao import infinitepay

    try:
        url, slug = infinitepay.criar_link(cobranca, descricao)
    except infinitepay.ErroCheckout:
        cobranca.status = cobranca.Status.CANCELADA
        cobranca.mensagem = "Falha ao gerar o link de pagamento."
        cobranca.save(update_fields=["status", "mensagem"])
        raise
    cobranca.link_url = url
    cobranca.slug = slug or ""
    cobranca.save(update_fields=["link_url", "slug"])
    return cobranca


def confirmar_cobranca(cobranca_id, transaction_nsu="", slug="", levantar_erro=False):
    """Confirma no provedor (nunca confiamos só no aviso) e processa. Idempotente.
    levantar_erro=True: repassa falhas de comunicação (o webhook usa para pedir reenvio)."""
    from .integracao import infinitepay
    from .models import Cobranca

    cobranca = Cobranca.objects.select_related("usuario", "servico").get(pk=cobranca_id)
    if cobranca.status == Cobranca.Status.PAGA:
        _emitir_pedido_pago(cobranca.pk)
        return Cobranca.objects.get(pk=cobranca.pk)
    transaction_nsu = transaction_nsu or cobranca.transaction_nsu
    slug = slug or cobranca.slug
    if transaction_nsu or slug:
        Cobranca.objects.filter(pk=cobranca.pk).update(
            transaction_nsu=(transaction_nsu or "")[:80], slug=(slug or "")[:80]
        )
    try:
        dados = infinitepay.verificar_pagamento(cobranca, transaction_nsu, slug)
    except infinitepay.ErroCheckout:
        if levantar_erro:
            raise
        return Cobranca.objects.get(pk=cobranca.pk)
    if not dados or not dados.get("paid"):
        return Cobranca.objects.get(pk=cobranca.pk)
    _registrar_pagamento(cobranca.pk, dados)
    _emitir_pedido_pago(cobranca.pk)
    return Cobranca.objects.get(pk=cobranca.pk)


def _registrar_pagamento(cobranca_id, dados):
    from .models import Cobranca

    with transaction.atomic():
        c = Cobranca.objects.select_for_update().get(pk=cobranca_id)
        if c.status == Cobranca.Status.PAGA:
            return False
        valor_cobrado = int(dados.get("amount") or 0)
        credito = min(valor_cobrado, c.valor_centavos) if valor_cobrado else c.valor_centavos
        if valor_cobrado and valor_cobrado != c.valor_centavos:
            log.warning("Cobrança %s: amount %s difere do esperado %s", c.pk, valor_cobrado, c.valor_centavos)
        if c.tipo == Cobranca.Tipo.PUBLICO:
            if valor_cobrado and valor_cobrado < c.valor_centavos:
                c.mensagem = "Valor pago menor que o da consulta. Fale com a gente pelo chat."
                c.status = Cobranca.Status.PAGA
                c.pago_em = timezone.now()
                c.valor_pago_centavos = dados.get("paid_amount") or valor_cobrado
                c.save()
                return True
        else:
            u = Usuario.objects.select_for_update().get(pk=c.usuario_id)
            forma = {"pix": "Pix", "credit_card": "cartão"}.get(dados.get("capture_method"), "online")
            _movimentar(u, Movimentacao.Tipo.PAGAMENTO, credito, f"Pagamento via {forma}")
        c.status = Cobranca.Status.PAGA
        c.pago_em = timezone.now()
        c.capture_method = str(dados.get("capture_method") or "")[:20]
        c.valor_pago_centavos = dados.get("paid_amount") or valor_cobrado or None
        c.mensagem = "Pagamento confirmado."
        c.save()
    if c.usuario_id and settings.BONUS_INDICACAO_GATILHO == "primeira_recarga":
        pagar_bonus_indicacao(c.usuario_id)
    return True


def _emitir_pedido_pago(cobranca_id):
    """Depois de pago, faz a consulta automaticamente (uma única vez)."""
    from .models import Cobranca

    with transaction.atomic():
        c = Cobranca.objects.select_for_update().select_related("usuario", "servico").get(pk=cobranca_id)
        if c.tipo == Cobranca.Tipo.PUBLICO and c.status == Cobranca.Status.PAGA and not c.pedido_id:
            if "menor" in c.mensagem:
                return c
            pedido = _criar_pedido_publico(c)
            c.pedido = pedido
            c.mensagem = "Pagamento confirmado e consulta solicitada."
            c.save(update_fields=["pedido", "mensagem"])
            return c
        if c.tipo != Cobranca.Tipo.PEDIDO or c.status != Cobranca.Status.PAGA or c.pedido_id:
            return c
        if not c.servico.ativo:
            c.mensagem = "Pagamento confirmado. O serviço está indisponível agora; o valor ficou no seu saldo."
            c.save(update_fields=["mensagem"])
            return c
        try:
            pedido = criar_pedido(c.usuario, c.servico, c.placa)
        except (SaldoInsuficiente, ValueError) as exc:
            c.mensagem = f"Pagamento confirmado e creditado no saldo, mas a consulta não foi feita: {exc}"
            c.save(update_fields=["mensagem"])
            return c
        c.pedido = pedido
        c.mensagem = "Pagamento confirmado e consulta solicitada."
        c.save(update_fields=["pedido", "mensagem"])
        return c


def sincronizar_cobrancas(limite=100):
    """Contingência: reconfere cobranças pendentes que já têm slug e transaction_nsu."""
    from .models import Cobranca

    pendentes = Cobranca.objects.filter(status=Cobranca.Status.PENDENTE).exclude(slug="").exclude(transaction_nsu="")
    total = 0
    for c in pendentes[:limite]:
        confirmar_cobranca(c.pk)
        total += 1
    return total


# --- Site público (cliente final, sem login) ----------------------------------

def iniciar_checkout_publico(servico, placa, nome, email, telefone):
    import secrets

    from .models import Cobranca

    placa = normalizar_placa(placa)
    if not servico.publico_ativo or servico.preco_publico_centavos < 100:
        raise ValueError("Serviço indisponível no momento.")
    cobranca = Cobranca.objects.create(
        tipo=Cobranca.Tipo.PUBLICO, valor_centavos=servico.preco_publico_centavos, servico=servico, placa=placa,
        comprador_nome=nome, comprador_email=email, comprador_telefone=telefone,
        token_acesso=secrets.token_urlsafe(32),
    )
    return _gerar_link(cobranca, f"{servico.nome} - placa {placa}")


def _criar_pedido_publico(cobranca):
    pedido = Pedido.objects.create(
        origem=Pedido.Origem.PUBLICO,
        servico=cobranca.servico,
        placa=cobranca.placa,
        valor_centavos=cobranca.valor_centavos,
        comprador_nome=cobranca.comprador_nome,
        comprador_email=cobranca.comprador_email,
        comprador_telefone=cobranca.comprador_telefone,
        token_acesso=cobranca.token_acesso,
        chave_idempotencia=f"pub-{uuid.uuid4().hex}",
        mensagem="Pagamento confirmado. Consulta em andamento.",
    )
    enviar_pedido(pedido)
    pedido.refresh_from_db()
    if pedido.aberto:  # demorado (ex.: CRLV): avisa que recebeu; o resultado vai em outro e-mail
        transaction.on_commit(lambda: notificar_publico(pedido.pk))
    return pedido


def notificar_publico(pedido_id):
    """E-mail para o comprador do site público com o link de acesso. Nunca derruba o fluxo."""
    from .emails import enviar_aviso_pedido_publico

    try:
        pedido = Pedido.objects.select_related("servico").get(pk=pedido_id)
        if pedido.comprador_email and pedido.token_acesso:
            enviar_aviso_pedido_publico(pedido)
    except Exception:  # noqa: BLE001
        log.exception("Falha ao avisar comprador do pedido %s", pedido_id)


@transaction.atomic
def marcar_reembolsado(pedido_id):
    pedido = Pedido.objects.select_for_update().get(pk=pedido_id)
    if not pedido.reembolso_pendente:
        raise ValueError("Este pedido não tem reembolso pendente.")
    pedido.reembolso_pendente = False
    pedido.reembolsado_em = timezone.now()
    pedido.mensagem = "Não foi possível concluir. O valor foi estornado."
    pedido.save(update_fields=["reembolso_pendente", "reembolsado_em", "mensagem"])
    return pedido
