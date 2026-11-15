"""E-mails transacionais (enviados via Resend/SMTP configurado no settings)."""

from html import escape

from django.conf import settings
from django.core.mail import EmailMultiAlternatives


def _nome_email():
    nome = settings.NOME_SISTEMA.strip()
    i = nome.rfind(" ")
    if i <= 0:
        return escape(nome)
    return f'{escape(nome[:i])} <span style="color:#b9c6f5">{escape(nome[i + 1:])}</span>'


def _html(titulo, saudacao, paragrafos, botao_texto, botao_link, rodape):
    p = "".join(f'<p style="margin:0 0 16px;font-size:15px;line-height:1.6;color:#334155">{t}</p>' for t in paragrafos)
    return f"""<!doctype html>
<html lang="pt-BR"><body style="margin:0;padding:0;background:#f5f7fb;font-family:Inter,Segoe UI,Roboto,Arial,sans-serif">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f5f7fb;padding:32px 16px">
<tr><td align="center">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:520px;background:#ffffff;border-radius:16px;overflow:hidden;border:1px solid #e6eaf1">
    <tr><td style="background-color:#0b1f5c;padding:22px 32px;color:#ffffff;font-family:Barlow,Arial,sans-serif;font-size:20px;font-weight:800;border-bottom:4px dashed #f2b705">
      {_nome_email()}
    </td></tr>
    <tr><td style="padding:32px">
      <h1 style="margin:0 0 16px;font-size:22px;color:#0f172a">{escape(titulo)}</h1>
      <p style="margin:0 0 16px;font-size:15px;color:#334155">{escape(saudacao)}</p>
      {p}
      <table role="presentation" cellpadding="0" cellspacing="0" style="margin:8px 0 24px"><tr><td style="border-radius:10px;background:#1d44d8">
        <a href="{escape(botao_link)}" style="display:inline-block;padding:14px 28px;color:#ffffff;font-weight:700;font-size:15px;text-decoration:none">{escape(botao_texto)}</a>
      </td></tr></table>
      <p style="margin:0 0 8px;font-size:13px;color:#64748b">Se o botão não funcionar, copie e cole este endereço no navegador:</p>
      <p style="margin:0 0 24px;font-size:12px;word-break:break-all;color:#1d44d8">{escape(botao_link)}</p>
      <p style="margin:0;font-size:13px;color:#64748b;border-top:1px solid #e6eaf1;padding-top:16px">{rodape}</p>
    </td></tr>
  </table>
</td></tr></table>
</body></html>"""


def enviar_redefinicao_senha(usuario, link):
    nome = (usuario.first_name or "").split(" ")[0] or "cliente"
    suporte = settings.SUPORTE_EMAIL
    texto = (
        f"Olá, {nome}!\n\n"
        "Recebemos um pedido para redefinir a senha da sua conta.\n"
        f"Para criar uma nova senha, acesse o link abaixo (válido por 2 horas):\n\n{link}\n\n"
        "Se você não fez esse pedido, ignore este e-mail: sua senha continua a mesma.\n"
        + (f"\nDúvidas? Fale com a gente: {suporte}\n" if suporte
           else "\nEsta é uma mensagem automática, não responda. Dúvidas? Fale com a gente pelo chat do sistema.\n")
    )
    rodape = "Se você não pediu a troca de senha, ignore este e-mail: sua senha continua a mesma."
    if suporte:
        rodape += f' Dúvidas? <a href="mailto:{escape(suporte)}" style="color:#1d44d8">{escape(suporte)}</a>'
    else:
        rodape += "<br><br>Esta é uma mensagem automática, não responda. Dúvidas? Fale com a gente pelo chat do sistema."
    html = _html(
        "Redefinir sua senha",
        f"Olá, {nome}!",
        ["Recebemos um pedido para redefinir a senha da sua conta. Clique no botão abaixo para criar uma nova senha.",
         "Por segurança, o link vale por <strong>2 horas</strong> e só pode ser usado uma vez."],
        "Criar nova senha",
        link,
        rodape,
    )
    msg = EmailMultiAlternatives(
        subject=f"Redefinição de senha · {settings.NOME_SISTEMA}",
        body=texto,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[usuario.email],
        reply_to=[suporte] if suporte else None,
    )
    msg.attach_alternative(html, "text/html")
    msg.send()


def enviar_aviso_pedido_publico(pedido):
    """Avisa o comprador do site público (pagamento recebido, resultado pronto ou falha)."""
    link = f"{settings.FRONTEND_URL}/c/{pedido.token_acesso}"
    nome = (pedido.comprador_nome or "").split(" ")[0] or "cliente"
    servico, placa = pedido.servico.nome, pedido.placa
    if pedido.status == "SUCESSO":
        assunto = f"Pronto: {servico} da placa {placa}"
        titulo = "Sua consulta está pronta"
        paragrafos = [f"A consulta <strong>{escape(servico)}</strong> da placa <strong>{escape(placa)}</strong> foi concluída.",
                      "Clique no botão para ver o resultado" + (" e baixar o documento." if pedido.documento else ".")]
        botao = "Ver resultado"
    elif pedido.status in ("FALHA", "SEM_RESULTADO"):
        assunto = f"Não foi possível concluir: {servico} · {placa}"
        titulo = "Não conseguimos concluir sua consulta"
        paragrafos = [f"A consulta <strong>{escape(servico)}</strong> da placa <strong>{escape(placa)}</strong> não pôde ser concluída.",
                      "O valor pago será estornado para a mesma forma de pagamento. Se tiver dúvidas, fale com a gente pelo chat do site."]
        botao = "Ver detalhes"
    else:
        assunto = f"Pagamento confirmado: {servico} · {placa}"
        titulo = "Recebemos seu pagamento"
        paragrafos = [f"Seu pedido <strong>{escape(servico)}</strong> da placa <strong>{escape(placa)}</strong> está em andamento.",
                      "O CRLV-e é emitido pelo órgão do estado e costuma levar de 1 a 3 dias úteis. Avisaremos por e-mail quando ficar pronto."
                      if pedido.servico.assincrono else "Você pode acompanhar pelo botão abaixo."]
        botao = "Acompanhar pedido"
    rodape = ("Guarde este e-mail: o link acima dá acesso ao seu resultado. Não compartilhe com outras pessoas.<br><br>"
              "Esta é uma mensagem automática, não responda. Dúvidas? Fale com a gente pelo chat do site.")
    texto = f"Olá, {nome}!\n\n{titulo}.\nAcesse: {link}\n\nNão compartilhe este link. Mensagem automática, não responda.\n"
    msg = EmailMultiAlternatives(subject=f"{assunto} · {settings.NOME_SISTEMA}", body=texto,
                                 from_email=settings.DEFAULT_FROM_EMAIL, to=[pedido.comprador_email])
    msg.attach_alternative(_html(titulo, f"Olá, {nome}!", paragrafos, botao, link, rodape), "text/html")
    msg.send()


def enviar_links_por_placa(email, pedidos):
    """Reenvia os links de acesso dos pedidos de uma placa para o e-mail usado na compra."""
    itens = "".join(
        f'<li style="margin:0 0 8px"><a href="{escape(settings.FRONTEND_URL)}/c/{escape(p.token_acesso)}" '
        f'style="color:#1d44d8">{escape(p.servico.nome)}</a> · {p.criado_em:%d/%m/%Y}</li>' for p in pedidos
    )
    placa = pedidos[0].placa
    html = _html("Seus links de acesso", "Olá!", [f"Estes são os pedidos da placa <strong>{escape(placa)}</strong> feitos com este e-mail:",
                                                   f'<ul style="padding-left:18px">{itens}</ul>'],
                 "Abrir o site", settings.FRONTEND_URL, "Se você não pediu este e-mail, ignore. Mensagem automática, não responda.")
    texto = "Seus pedidos:\n" + "\n".join(f"{p.servico.nome}: {settings.FRONTEND_URL}/c/{p.token_acesso}" for p in pedidos)
    msg = EmailMultiAlternatives(subject=f"Seus links de acesso · placa {placa}", body=texto,
                                 from_email=settings.DEFAULT_FROM_EMAIL, to=[email])
    msg.attach_alternative(html, "text/html")
    msg.send()
