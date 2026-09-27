from app.errors import friendly, hint


class Orig(Exception):
    pass


class Wrapped(Exception):
    def __init__(self, orig):
        super().__init__("wrapper")
        self.orig = orig


def test_pg_hba_french_garbled():
    # Message réel d'un PostgreSQL Windows en français (accents perdus).
    msg = ('connection failed: connection to server at "10.192.140.65", port 5432 failed: FATAL:  aucune entr�e '
           "dans pg_hba.conf pour l'h�te � 10.192.140.65 �, utilisateur � postgres �, "
           "base de donn�es � point �, aucun chiffrement")
    text = friendly(Wrapped(Orig(msg + "\n(Background on this error at: https://sqlalche.me/e/21/e3q8)")))
    assert "utilisez l'hôte « localhost »" in text
    assert "host all all 10.192.140.65/32 scram-sha-256" in text
    assert "Background" not in text


def test_pg_hba_english():
    msg = 'FATAL:  no pg_hba.conf entry for host "192.168.1.20", user "sync", database "dwh", no encryption'
    assert "host all all 192.168.1.20/32" in hint(msg)


def test_other_hints():
    assert "mot de passe incorrect" in hint('FATAL:  password authentication failed for user "sync"')
    assert "mot de passe incorrect" in hint("(1045, \"Access denied for user 'sync'@'10.0.0.2'\")")
    assert "@'%'" in hint("(1130, \"Host '10.0.0.2' is not allowed to connect to this MariaDB server\")")
    assert "n'existe pas" in hint('FATAL:  database "dwh2" does not exist')
    assert "n'existe pas" in hint("(1049, \"Unknown database 'x'\")")
    assert "Serveur injoignable" in hint("connection failed: Connection refused")
    assert "Serveur injoignable" in hint("(2003, \"Can't connect to MySQL server on 'x' (10061)\")")
    assert hint('relation "t" does not exist') == ""
