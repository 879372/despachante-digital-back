# despachante-digital-back

Backend Django REST do Despachante Digital. Frontend: https://github.com/879372/despachante-digital-front

Os comandos Python abaixo devem ser executados na raiz deste repositório. Os comandos npm pertencem ao repositório frontend.

# Despachante Digital — CRLV-e e Código de Segurança do CRV

React (Vite) + Django REST. A integração com a fonte de dados fica **só no backend**:
o navegador conversa apenas com `/api/...` do seu servidor e nunca vê URL, chave,
nome de produto (SKU) nem mensagens cruas do fornecedor.

## Como rodar (desenvolvimento)

```bash
cp .env.example .env          # configure a integração e os pagamentos
pip install -r requirements.txt
python manage.py migrate      # já cria os serviços "CRLV-e RN" e "Código de Segurança"
python manage.py createsuperuser
python manage.py runserver
```

```bash
npm install
npm run dev                   # http://localhost:5173 (proxy de /api para o Django)
```

## Serviços e mapeamento interno

| Serviço (frontend) | Código interno | Produto na fonte | Tipo |
|---|---|---|---|
| CRLV-e RN | `crlv-rn` | `veicular-crlv-rn-sf` | Assíncrono (1–3 dias úteis), retorna PDF |
| Código de Segurança do CRV | `codigo-seguranca` | `veicular-codigo-seguranca-crv-sf` | Síncrono |

Preços de venda (padrão R$ 50,00 e R$ 30,00) são editáveis em **Gestão → Serviços**.

## Fluxo financeiro (duas carteiras)

1. **Carteira do despachante na fonte** (pré-paga): recarregada **no painel do fornecedor**,
   não pela API — a API só *lê* o saldo (`GET /v1/saldo`), que aparece em Gestão → Resumo.
   Sem saldo lá, os pedidos falham (HTTP 402) e o cliente recebe estorno automático.
2. **Carteira de cada cliente no sistema**: o saldo entra **só pelo checkout da InfinitePay** (Pix ou cartão),
   creditado automaticamente quando o pagamento é confirmado.
   Cada pedido debita o preço de venda; falhas são estornadas (`ESTORNAR_FALHAS=1`).

## Cadastro e indicação

- Cadastro público em `/cadastro`: nome e sobrenome, e-mail, CPF/CNPJ (dígitos verificadores
  conferidos), celular com DDD, senha forte e aceite dos termos. E-mail e CPF/CNPJ são únicos.
- Cada cliente tem um **código de indicação** (ex.: `MARIA8829`) e um link
  `https://SEU-DOMINIO/convite/CODIGO`, que abre o cadastro com o código preenchido.
- Quem indica e quem foi indicado ganham `BONUS_INDICACAO_CENTAVOS` (padrão R$ 5,00), **uma única vez**.
- Quando o bônus é liberado (`BONUS_INDICACAO_GATILHO`):
  - `primeira_recarga` (padrão): quando a 1ª recarga do indicado é aprovada. Evita contas falsas
    criadas só para ganhar bônus.
  - `cadastro`: assim que o indicado cria a conta.

## Esqueceu a senha

O cliente informa o e-mail e recebe um link (válido por 2 horas, uso único) para criar uma nova senha.
A resposta é sempre a mesma, exista ou não o e-mail, para não revelar quem tem cadastro.
O envio usa o **Resend** pelo domínio `kaibez.com.br` (SMTP `smtp.resend.com:587`, usuário `resend`).
Configure no `backend/.env`:

- `EMAIL_SENHA`: a API key do Resend (`re_...`). Vazio = o e-mail só aparece no terminal do Django.
- `EMAIL_REMETENTE`: padrão `Despachante Digital <nao-responda@kaibez.com.br>` (precisa ser do domínio verificado).
- `SUPORTE_EMAIL`: contato exibido na tela e no e-mail (vira também o "responder para").
- `FRONTEND_URL`: domínio usado no link do e-mail (em produção, o seu domínio real).

Para conferir se está enviando:

```bash
python manage.py testar_email voce@seuemail.com
```

## Site público (cliente final, sem login)

Quem acessa `/` sem estar logado cai no site público; a área dos despachantes fica em `/entrar`.

- **Fluxo:** digita a placa → vê as consultas já feitas para ela → escolhe o serviço → informa nome,
  e-mail e WhatsApp → paga no checkout (Pix ou cartão) → a consulta é feita assim que o pagamento é confirmado.
- **Preços próprios:** cada serviço tem preço e ativação separados para o painel e para o site
  (Gestão → Serviços e preços). Mínimo R$ 1,00 no site.
- **Privacidade:** pela placa, qualquer pessoa vê só serviço, data e situação. O resultado (código de
  segurança, PDF) só abre pelo **link de acesso** `/c/<token>`, salvo no aparelho do comprador e enviado
  ao e-mail dele (pagamento confirmado, resultado pronto ou falha). De outro aparelho: "Reenviar meu link"
  manda os links para o e-mail usado na compra.
- **Falha depois de pago:** o visitante não tem carteira, então o pedido fica como **reembolso pendente**
  (Gestão → Pedidos → Reembolso pendente). Estorne no app da InfinitePay e marque como reembolsado.
- **Chat:** balão flutuante para visitantes (nome + WhatsApp/e-mail). As conversas aparecem em
  Atendimento com a etiqueta "site".

## Pagamento online (InfinitePay · Checkout Integrado)

- **Cliente sem saldo na hora da consulta:** a tela mostra o valor da consulta, o saldo e **quanto falta**
  (ou o valor cheio, se o saldo for zero; mínimo R$ 1,00). O botão leva ao checkout (Pix ou cartão em até 12x).
  Quando o pagamento é confirmado, o valor entra na carteira e **a consulta é feita automaticamente**.
- **Carteira:** "Adicionar saldo" leva ao checkout. Não existe mais PIX manual com comprovante.
- **Quando o saldo entra:** assim que a InfinitePay avisa pelo webhook (e o pagamento é confirmado no
  `/payment_check`), ou quando o cliente volta do checkout, o que acontecer primeiro. Se a confirmação
  falhar por instabilidade, o webhook responde 400 e a InfinitePay reenvia o aviso; o cron
  `sincronizar_pedidos` também reconfere pagamentos pendentes.
- **Segurança:** o webhook da InfinitePay não é assinado, então nenhum aviso libera saldo sozinho:
  todo pagamento é confirmado no `/payment_check` oficial. O crédito usa o valor cobrado (`amount`),
  não o `paid_amount` (que inclui juros de parcelamento). Confirmação repetida não credita duas vezes.
- A primeira recarga paga online também libera o bônus de indicação.

Configure em `backend/.env`:

- `INFINITEPAY_HANDLE`: sua InfiniteTag sem o `$` (ative o *Checkout Integrado* nas Configurações do Checkout).
- `INFINITEPAY_API_KEY` (`inchk_...`, recomendado): links de uso único que expiram em `INFINITEPAY_VALIDADE_HORAS`.
- `INFINITEPAY_WEBHOOK_BASE`: URL pública HTTPS do backend em produção (ex.: `https://api.seudominio.com.br`).
  O aviso chega em `/api/pagamentos/retorno/<INFINITEPAY_WEBHOOK_TOKEN>/`.
- `FRONTEND_URL`: para onde o cliente volta depois de pagar (`/pagamento/retorno`).

Sem `INFINITEPAY_HANDLE`/`API_KEY`, o cliente não consegue adicionar saldo (a tela avisa que o pagamento
está indisponível). A equipe ainda pode ajustar saldo manualmente em Gestão → Clientes.
A InfinitePay **não tem ambiente de testes**: valide com uma compra real de R$ 1,00 e estorne pelo app.

## Arquivos (S3 privado)

PDFs do CRLV e anexos do chat ficam num **bucket S3 privado**.
O navegador nunca acessa o bucket: todo download passa por uma rota autenticada do backend, que confere
se o arquivo é do cliente (ou se é a equipe). Os arquivos são gravados com nome aleatório
(`documentos/2026/11/<uuid>.pdf`), sem o nome original nem dados pessoais no caminho.

### Criando o bucket na AWS

1. **S3 → Create bucket**: nome `kaibez-despachante-arquivos` (ou outro único), região **sa-east-1 (São Paulo)**.
2. **Block all public access**: deixe **ligado** (padrão). **Object Ownership**: *Bucket owner enforced* (ACLs desativadas).
3. **Default encryption**: SSE-S3. **Bucket Versioning**: recomendado ligar (protege contra exclusão acidental).
4. **IAM → Users → Create user** (ex.: `despachante-app`), sem acesso ao console, com esta política
   (troque o nome do bucket se mudou):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::kaibez-despachante-arquivos/privado/*"
    },
    {
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::kaibez-despachante-arquivos",
      "Condition": { "StringLike": { "s3:prefix": ["privado/*"] } }
    }
  ]
}
```

5. Crie uma **Access key** (tipo *Application running outside AWS*) e coloque em `S3_ACCESS_KEY` /
   `S3_SECRET_KEY` direto nas variáveis do Railway.
6. Não precisa configurar CORS nem bucket policy pública.

Para conferir: `python manage.py testar_storage` (grava, lê e apaga um arquivo de teste).
Também funciona com provedores compatíveis com S3 (Cloudflare R2, Backblaze) via `S3_ENDPOINT`.

## Chat interno de suporte

- **Cliente:** balão no canto da tela. Abre conversas, anexa PDF/imagem (até 5 MB) e pode iniciar
  uma conversa já vinculada a um pedido ("Falar sobre este pedido").
- **Equipe:** menu **Atendimento** com contador de não lidas, busca por cliente/CPF/placa, respostas
  rápidas e botão Resolver. Se o cliente escrever numa conversa resolvida, ela reabre sozinha.
- Atualização por consulta periódica (4 s com a conversa aberta, 15 s para o contador), sem
  serviço extra. Anexos ficam em `privado/` e só saem para o cliente dono ou para a equipe.

## Webhook (aviso de CRLV pronto)

No painel do fornecedor, em **Chaves**, cadastre:

```
https://SEU-DOMINIO/api/retorno/<WEBHOOK_TOKEN_ROTA>/
```

e cole o segredo mostrado em `FONTE_WEBHOOK_SECRET`. A assinatura HMAC é conferida;
o aviso só dispara uma consulta de status (a verdade vem sempre do GET).

Contingência (cron a cada 15 min):

```
*/15 * * * * cd /caminho/backend && python manage.py sincronizar_pedidos
```

## Produção

- `npm run build` e sirva `frontend/dist` pelo Nginx no **mesmo domínio** do Django;
  encaminhe `/api/` e `/gestao/` para o gunicorn (`gunicorn config.wsgi`).
- **Não** exponha a pasta `backend/privado/` (PDFs e comprovantes): eles só saem por
  endpoints autenticados.
- `DJANGO_DEBUG=0`, `DJANGO_SECRET_KEY` forte, `DATABASE_URL` para PostgreSQL.

## Testes

```bash
python manage.py test core
```
