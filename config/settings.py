import os
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent.parent


def _carregar_env():
    arquivo = BASE_DIR / ".env"
    if not arquivo.exists():
        return
    for linha in arquivo.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        chave, valor = linha.split("=", 1)
        os.environ.setdefault(chave.strip(), valor.strip().strip('"').strip("'"))


_carregar_env()
env = os.environ.get

SECRET_KEY = env("DJANGO_SECRET_KEY", "dev-inseguro-troque-em-producao")
DEBUG = env("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = [h.strip() for h in env("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "corsheaders",
    "core",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"


def _banco():
    url = env("DATABASE_URL", "")
    if not url:
        return {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3"}
    p = urlparse(url)
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": p.path.lstrip("/"),
        "USER": p.username,
        "PASSWORD": p.password,
        "HOST": p.hostname,
        "PORT": p.port or 5432,
    }


DATABASES = {"default": _banco()}

AUTH_USER_MODEL = "core.Usuario"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]

LANGUAGE_CODE = "pt-br"
TIME_ZONE = "America/Fortaleza"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# Arquivos privados (PDFs e comprovantes). Não são servidos publicamente:
# só saem por views autenticadas.
MEDIA_ROOT = Path(env("MEDIA_ROOT", str(BASE_DIR / "privado")))  # no Railway: volume persistente
MEDIA_URL = "/privado/"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Armazenamento de arquivos (PDFs, comprovantes, anexos do chat) ---
# Com S3_BUCKET definido, tudo vai para um bucket S3 PRIVADO. Sem ele, disco local (MEDIA_ROOT).
# O navegador nunca acessa o bucket: os downloads passam pelas rotas autenticadas do backend.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
if env("S3_BUCKET"):
    _s3 = {
        "bucket_name": env("S3_BUCKET"),
        "region_name": env("S3_REGIAO", "sa-east-1"),
        "access_key": env("S3_ACCESS_KEY"),
        "secret_key": env("S3_SECRET_KEY"),
        "location": env("S3_PREFIXO", "privado"),
        "default_acl": None,          # herda do bucket (privado)
        "file_overwrite": False,      # nunca sobrescreve um arquivo existente
        "querystring_auth": True,     # qualquer URL gerada é assinada e expira
        "querystring_expire": 300,
        "signature_version": "s3v4",
    }
    if env("S3_ENDPOINT"):            # compatível com S3 (Cloudflare R2, Backblaze, MinIO...)
        _s3["endpoint_url"] = env("S3_ENDPOINT")
    else:                             # AWS: criptografia em repouso
        _s3["object_parameters"] = {"ServerSideEncryption": "AES256"}
    STORAGES["default"] = {"BACKEND": "storages.backends.s3.S3Storage", "OPTIONS": _s3}

import sys  # noqa: E402

if sys.argv[1:2] == ["test"]:  # testes nunca tocam disco nem S3
    STORAGES["default"] = {"BACKEND": "django.core.files.storage.InMemoryStorage"}

CORS_ALLOWED_ORIGINS = [o.strip() for o in env("CORS_ORIGINS", "http://localhost:5173").split(",") if o.strip()]

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["rest_framework_simplejwt.authentication.JWTAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_THROTTLE_CLASSES": ["rest_framework.throttling.UserRateThrottle"],
    "DEFAULT_THROTTLE_RATES": {"user": "120/min", "pedidos": "20/min", "publico": "10/min", "chat": "30/min", "pagamentos": "10/min", "publico_leitura": "90/min"},
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 25,
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(hours=8),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
}

# --- Integração com a fonte de dados (somente backend) ---
FONTE_DADOS = {
    "URL": env("FONTE_API_URL", "https://api.buscars.com.br").rstrip("/"),
    "CHAVE": env("FONTE_API_KEY", ""),
    "SEGREDO_WEBHOOK": env("FONTE_WEBHOOK_SECRET", ""),
    "TIMEOUT": int(env("FONTE_TIMEOUT", "40")),
}
WEBHOOK_TOKEN_ROTA = env("WEBHOOK_TOKEN_ROTA", "troque-este-token")

ESTORNAR_FALHAS = env("ESTORNAR_FALHAS", "1") == "1"

# --- Programa de indicação ---
BONUS_INDICACAO_CENTAVOS = int(env("BONUS_INDICACAO_CENTAVOS", "500"))
# "primeira_recarga" = no 1º pagamento confirmado do indicado (recomendado) ou "cadastro"
BONUS_INDICACAO_GATILHO = env("BONUS_INDICACAO_GATILHO", "primeira_recarga")

# --- Pagamento online (Checkout Integrado InfinitePay) ---
INFINITEPAY = {
    "HANDLE": env("INFINITEPAY_HANDLE", "").lstrip("$").strip(),
    "API_KEY": env("INFINITEPAY_API_KEY", ""),
    # URL pública HTTPS do backend para receber o webhook (vazio em dev: confirma pelo retorno do cliente)
    "WEBHOOK_BASE": env("INFINITEPAY_WEBHOOK_BASE", ""),
    "WEBHOOK_TOKEN": env("INFINITEPAY_WEBHOOK_TOKEN", "troque-este-token-de-pagamento"),
    "VALIDADE_HORAS": int(env("INFINITEPAY_VALIDADE_HORAS", "2")),
}

# Até 2 anexos de 5 MB codificados em base64 no formulário de comunicado.
DATA_UPLOAD_MAX_MEMORY_SIZE = 15 * 1024 * 1024

# --- Comunicação de venda (segunda fonte; só backend) ---
CARVEN = {
    "URL": env("CARVEN_API_URL", "https://www.carven.com.br/api").rstrip("/"),
    "CLIENT_ID": env("CARVEN_CLIENT_ID", ""),
    "CLIENT_SECRET": env("CARVEN_CLIENT_SECRET", ""),
    "TIMEOUT": int(env("CARVEN_TIMEOUT", "40")),
    # URL pública do backend para o aviso de mudança de situação (vazio = só verificação periódica)
    "WEBHOOK_BASE": env("CARVEN_WEBHOOK_BASE", env("INFINITEPAY_WEBHOOK_BASE", "")),
    "WEBHOOK_TOKEN": env("CARVEN_WEBHOOK_TOKEN", ""),
}

# --- Login por e-mail ou usuário ---
AUTHENTICATION_BACKENDS = ["core.autenticacao.EmailOuUsuarioBackend"]

# --- E-mail (recuperação de senha) ---
FRONTEND_URL = env("FRONTEND_URL", "http://localhost:5173").rstrip("/")
SUPORTE_EMAIL = env("SUPORTE_EMAIL", "")
NOME_SISTEMA = env("NOME_SISTEMA", "Despachante Digital")
# Remetente precisa ser de um domínio verificado no Resend (kaibez.com.br)
DEFAULT_FROM_EMAIL = env("EMAIL_REMETENTE", "Despachante Digital <nao-responda@kaibez.com.br>")
SERVER_EMAIL = DEFAULT_FROM_EMAIL
EMAIL_TIMEOUT = 15
try:  # Python do macOS (python.org) não acha os certificados raiz do sistema
    import certifi

    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
except ImportError:  # pragma: no cover
    pass
if env("EMAIL_SENHA"):
    # HTTPS evita depender de conexões SMTP de saída na hospedagem.
    EMAIL_BACKEND = "core.email_backend.ResendBackend"
    EMAIL_HOST = env("EMAIL_HOST", "smtp.resend.com")
    EMAIL_PORT = int(env("EMAIL_PORT", "587"))
    EMAIL_HOST_USER = env("EMAIL_USUARIO", "resend")
    EMAIL_HOST_PASSWORD = env("EMAIL_SENHA")
    EMAIL_USE_TLS = EMAIL_PORT == 587
    EMAIL_USE_SSL = EMAIL_PORT == 465
else:
    # Links privados só aparecem no terminal em desenvolvimento.
    EMAIL_BACKEND = ("django.core.mail.backends.console.EmailBackend" if DEBUG
                     else "django.core.mail.backends.dummy.EmailBackend")
PASSWORD_RESET_TIMEOUT = 60 * 60 * 2  # link vale 2 horas

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "loggers": {"core": {"handlers": ["console"], "level": "INFO"}},
}

CSRF_TRUSTED_ORIGINS = [o.strip() for o in env("CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()]

if not DEBUG:
    from django.core.exceptions import ImproperlyConfigured

    if SECRET_KEY.startswith("dev-") or len(SECRET_KEY) < 40:
        raise ImproperlyConfigured("Defina DJANGO_SECRET_KEY forte em produção.")
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = int(env("SECURE_HSTS_SECONDS", "31536000"))
    SECURE_REFERRER_POLICY = "same-origin"
    X_FRAME_OPTIONS = "DENY"
