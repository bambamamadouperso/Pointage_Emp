"""Récupère la clé de chiffrement des mots de passe des connexions après la perte du fichier .env.

Les mots de passe des connexions sont chiffrés avec SECRET_KEY (ou ENCRYPTION_KEY). Si le .env a été
abîmé puis recréé, la clé a changé et les connexions ne s'ouvrent plus. Cet outil cherche les anciennes
clés dans les copies du .env (.env.abime-*, .env.*), essaie chacune sur les mots de passe enregistrés
et remet dans .env celle qui fonctionne.

Codes de sortie : 0 = clé correcte (déjà en place ou récupérée), 1 = aucune clé trouvée, 2 = rien à faire
(aucun mot de passe chiffré).
"""
import base64
import glob
import hashlib
import os
import re
import sys

from cryptography.fernet import Fernet, InvalidToken

_KEY_RE = re.compile(rb"(SECRET_KEY|ENCRYPTION_KEY)[ \t]*=[ \t]*['\"]?([^\s'\"\x00]{8,200})")
_CHUNK = 1 << 20


def say(message: str) -> None:
    print(message, flush=True)


def fernet_for(kind: str, value: str) -> Fernet:
    if kind == "ENCRYPTION_KEY":
        return Fernet(value.encode())
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(value.encode()).digest()))


def works(kind: str, value: str, token: str) -> bool:
    try:
        fernet_for(kind, value).decrypt(token.encode())
        return True
    except (InvalidToken, ValueError, TypeError):
        return False


def candidates(path: str) -> list[tuple[str, str]]:
    """Clés trouvées dans un fichier, lu par morceaux (un fichier abîmé peut être énorme)."""
    found: list[tuple[str, str]] = []
    tail = b""
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(_CHUNK)
                if not chunk:
                    break
                data = tail + chunk
                for m in _KEY_RE.finditer(data):
                    item = (m.group(1).decode(), m.group(2).decode("utf-8", "ignore"))
                    if item not in found:
                        found.append(item)
                tail = data[-300:]
    except OSError as exc:
        say(f"    Lecture impossible de {os.path.basename(path)} : {exc}")
    return found


def set_env(env_file: str, kind: str, value: str) -> None:
    """Remplace la clé dans .env (une ancienne SECRET_KEY remplace aussi une ENCRYPTION_KEY éventuelle)."""
    with open(env_file, encoding="utf-8-sig") as f:
        lines = [line.rstrip("\r\n") for line in f]
    names = "SECRET_KEY|ENCRYPTION_KEY" if kind == "SECRET_KEY" else "ENCRYPTION_KEY"
    kept = [line for line in lines if not re.match(rf"^\s*({names})\s*=", line)]
    with open(env_file, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(kept + [f"{kind}={value}"]) + "\n")


def find_best(files: list[str], tokens: list[tuple[str, str]], current: tuple[str, str]):
    """Clé qui déchiffre le plus de connexions (certaines ont pu être ressaisies avec la nouvelle clé)."""
    best, best_ok = None, [name for name, t in tokens if works(*current, t)]
    for path in files:
        say(f"Recherche dans {os.path.basename(path)}...")
        for kind, value in candidates(path):
            ok = [name for name, t in tokens if works(kind, value, t)]
            if len(ok) > len(best_ok):
                best, best_ok = (kind, value), ok
    return best, best_ok


def main() -> int:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.chdir(root)
    env_file = os.path.join(root, ".env")
    try:
        from dotenv import load_dotenv

        load_dotenv(env_file)
    except ImportError:
        pass

    from .config import settings
    from .database import SessionLocal, init_db
    from .models import Connection

    init_db()
    with SessionLocal() as db:
        tokens = [(c.name, c.password_enc) for c in db.query(Connection).order_by(Connection.id) if c.password_enc]
    if not tokens:
        say("Aucun mot de passe de connexion enregistré : rien à récupérer.")
        return 2

    current = ("ENCRYPTION_KEY", settings.encryption_key) if settings.encryption_key \
        else ("SECRET_KEY", settings.secret_key)
    if all(works(*current, t) for _, t in tokens):
        say("La clé actuelle déchiffre bien les mots de passe des connexions : rien à faire.")
        return 0

    files = sorted(set(glob.glob(os.path.join(root, ".env.*")) + glob.glob(os.path.join(root, ".env-*"))),
                   key=os.path.getmtime, reverse=True)
    files = [p for p in files if not p.endswith(".example")]
    if not files:
        say("Aucune copie de l'ancien fichier .env trouvée (.env.abime-*).")
    best, best_ok = find_best(files, tokens, current)
    if best:
        set_env(env_file, *best)
        say(f"    Ancienne clé retrouvée ({best[0]}) : elle déchiffre {len(best_ok)}/{len(tokens)} connexion(s).")
        say("    Elle a été remise dans le fichier .env.")
        missing = [name for name, _ in tokens if name not in best_ok]
        if missing:
            say("    Mot de passe à ressaisir (Connexions -> Modifier) pour : " + ", ".join(missing))
        return 0
    say("")
    say("Aucune ancienne clé ne déchiffre les mots de passe.")
    say("Ressaisissez le mot de passe de chaque connexion : Connexions -> Modifier -> Mot de passe -> Enregistrer.")
    for name, _ in tokens:
        say(f"    - {name}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
