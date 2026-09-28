"""Messages d'erreur de connexion compréhensibles, avec la marche à suivre.

Les messages des serveurs peuvent être traduits (et mal encodés sous Windows : « entr�e ») :
on ne s'appuie donc que sur des fragments stables (pg_hba.conf, codes MySQL, mots anglais et français).
"""
import re

_IP = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3})")


def raw_message(exc: BaseException) -> str:
    msg = str(getattr(exc, "orig", None) or exc).strip()
    msg = msg.split("(Background on this error")[0].strip()
    return msg.splitlines()[0] if msg else exc.__class__.__name__


def _client_ip(msg: str) -> str:
    """Adresse du client citée par PostgreSQL (après « pg_hba.conf »), sinon la première trouvée."""
    after = msg.split("pg_hba.conf", 1)[-1]
    match = _IP.search(after) or _IP.search(msg)
    return match.group(1) if match else "ADRESSE_DU_CLIENT"


def _user(msg: str) -> str:
    match = re.search(r"(?:user|utilisateur)\W+([A-Za-z0-9_.-]+)", msg)
    return match.group(1) if match else "UTILISATEUR"


def hint(msg: str) -> str:
    low = msg.lower()
    if "statement timeout" in low or "délai d'attente de la requête" in low:
        return ("La base PostgreSQL a mis trop de temps à répondre (plus de 2 minutes) : réduisez la période "
                "ou les filtres, puis réessayez.")
    if "lock timeout" in low or "canceling statement due to lock" in low or "verrou" in low:
        return ("La base PostgreSQL est occupée (table verrouillée par une synchronisation ou un autre "
                "programme) : réessayez dans quelques instants.")
    if "pg_hba.conf" in low:
        ip = _client_ip(msg)
        return (
            f"PostgreSQL refuse les connexions venant de {ip} (fichier pg_hba.conf). "
            f"Si PostgreSQL est installé sur ce même serveur, utilisez l'hôte « localhost ». "
            f"Sinon, ajoutez à la fin de pg_hba.conf la ligne « host all all {ip}/32 scram-sha-256 » "
            f"puis rechargez PostgreSQL (services.msc → redémarrer, ou SELECT pg_reload_conf();)."
        )
    if "password authentication" in low or "authentification par mot de passe" in low \
            or "(1045" in low or "access denied" in low:
        return "Utilisateur ou mot de passe incorrect."
    if "is not allowed to connect" in low or "(1130" in low:
        return (
            f"MariaDB n'autorise pas cet utilisateur depuis cette machine. En tant qu'administrateur : "
            f"CREATE USER '{_user(msg)}'@'%' IDENTIFIED BY '…'; GRANT SELECT ON base.* TO '{_user(msg)}'@'%';"
        )
    if re.search(r"(database|base de donn\S*)\s+\W?\S+\W?\s+(does not exist|n.existe pas)", low) \
            or "unknown database" in low or "(1049" in low:
        return "Cette base n'existe pas : choisissez-la avec « Lister les bases » ou créez-la."
    if "connection refused" in low or "connexion refus" in low or "10061" in low or "(2003" in low:
        return (
            "Serveur injoignable : vérifiez l'hôte et le port, que le service est démarré, "
            "qu'il écoute sur le réseau (listen_addresses / bind-address) et le pare-feu."
        )
    if "timeout" in low or "timed out" in low or "délai" in low:
        return "Le serveur ne répond pas (délai dépassé) : vérifiez l'adresse, le réseau et le pare-feu."
    if "could not translate host name" in low or "name or service not known" in low \
            or "getaddrinfo" in low or "nodename nor servname" in low:
        return "Nom d'hôte inconnu : vérifiez l'adresse du serveur."
    return ""


def friendly(exc: BaseException) -> str:
    """Explication + message d'origine du serveur."""
    msg = raw_message(exc)
    advice = hint(msg)
    return f"{advice} (Détail : {msg[:400]})" if advice else msg[:1000]
