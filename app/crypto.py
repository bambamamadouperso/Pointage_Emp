"""Chiffrement des mots de passe des connexions stockés dans la base interne."""
import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from .config import settings


def _fernet() -> Fernet:
    key = settings.encryption_key
    if not key:
        digest = hashlib.sha256(settings.secret_key.encode()).digest()
        key = base64.urlsafe_b64encode(digest).decode()
    return Fernet(key.encode())


def encrypt(value: str) -> str:
    if not value:
        return ""
    return _fernet().encrypt(value.encode()).decode()


def decrypt(token: str) -> str:
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise ValueError(
            "Impossible de déchiffrer le mot de passe : SECRET_KEY/ENCRYPTION_KEY a changé. "
            "Ressaisissez le mot de passe de la connexion."
        ) from exc
