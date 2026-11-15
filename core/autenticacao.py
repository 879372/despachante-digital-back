from django.contrib.auth.backends import ModelBackend

from .models import Usuario


class EmailOuUsuarioBackend(ModelBackend):
    """Permite entrar com o e-mail ou com o nome de usuário."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        if not username or not password:
            return None
        login = username.strip()
        usuario = (
            Usuario.objects.filter(email__iexact=login).order_by("id").first()
            if "@" in login
            else Usuario.objects.filter(username__iexact=login).first()
        )
        if usuario is None:
            Usuario().set_password(password)  # tempo constante contra enumeração
            return None
        if usuario.check_password(password) and self.user_can_authenticate(usuario):
            return usuario
        return None
