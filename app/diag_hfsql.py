"""Diagnostic de connexion HFSQL, à lancer sur le serveur dans une session Windows ouverte.

    .venv\\Scripts\\python.exe -m app.diag_hfsql "Nom de la connexion"

Lancé dans la session de l'utilisateur (et non en tâche de fond), il rend visible une éventuelle fenêtre
ouverte par le pilote ODBC, et affiche chaque étape avec sa durée. Le rapport est aussi écrit dans
logs\\diagnostic-hfsql.txt.
"""
import os
import platform
import struct
import sys
import time

REPORT: list[str] = []


def say(text: str = "") -> None:
    print(text, flush=True)
    REPORT.append(text)


def step(title: str, fn, timeout: float):
    from .netcheck import NetError, call_with_timeout

    say(f"\n>>> {title} (délai max. {timeout:.0f} s)")
    start = time.monotonic()
    try:
        result = call_with_timeout(fn, timeout, f"aucune réponse après {timeout:.0f} s")
        say(f"    OK en {time.monotonic() - start:.1f} s")
        return True, result
    except NetError as exc:
        say(f"    BLOQUÉ : {exc}")
    except Exception as exc:  # noqa: BLE001
        say(f"    ERREUR après {time.monotonic() - start:.1f} s : {exc}")
    return False, None


def main() -> int:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.chdir(root)
    try:
        from dotenv import load_dotenv

        load_dotenv(os.path.join(root, ".env"))
    except ImportError:
        pass

    from . import hfsql
    from .database import SessionLocal, init_db
    from .models import Connection

    init_db()
    with SessionLocal() as db:
        conns = db.query(Connection).filter_by(kind="hfsql").order_by(Connection.name).all()
    if not conns:
        say("Aucune connexion HFSQL enregistrée : créez-la d'abord dans l'application (Connexions).")
        return 1
    wanted = " ".join(sys.argv[1:]).strip()
    conn = next((c for c in conns if c.name == wanted), None) if wanted else conns[0]
    if conn is None:
        say(f"Connexion « {wanted} » introuvable. Connexions HFSQL : {', '.join(c.name for c in conns)}")
        return 1

    say(f"Diagnostic HFSQL — connexion « {conn.name} » ({conn.host}:{conn.port}, base {conn.database})")
    say(f"Python {platform.python_version()} {struct.calcsize('P') * 8} bits — {platform.platform()}")
    try:
        pyodbc = hfsql._pyodbc()
    except hfsql.HfsqlError as exc:
        say(f"ERREUR : {exc}")
        return 1
    say(f"pyodbc {pyodbc.version} — pilotes ODBC {struct.calcsize('P') * 8} bits installés :")
    for d in pyodbc.drivers():
        say(f"    {'->' if any(h in d.lower() for h in hfsql.DRIVER_HINTS) else '  '} {d}")

    try:
        cs = hfsql.connection_string(conn)
    except hfsql.HfsqlError as exc:
        say(f"\nERREUR : {exc}")
        return finish(root)
    say(f"\nChaîne de connexion : {hfsql.masked(cs)}")

    from .netcheck import check_port

    ok, _ = step(f"Port TCP {conn.host}:{conn.port}", lambda: check_port(conn.host, conn.port), 10)
    if not ok:
        return finish(root)

    say("\n    Si une fenêtre du pilote HFSQL s'ouvre maintenant, notez ce qu'elle demande :")
    say("    c'est elle qui bloque l'application (qui tourne sans fenêtre, en tâche de fond).")
    ok, cnx = step("Connexion ODBC", lambda: pyodbc.connect(cs, autocommit=True), 300)
    if not ok:
        return finish(root)
    for code, label in ((hfsql.SQL_DBMS_NAME, "Serveur"), (hfsql.SQL_DBMS_VER, "Version")):
        try:
            say(f"    {label} : {cnx.getinfo(code)}")
        except Exception:
            pass

    def list_tables():
        cur = cnx.cursor()
        try:
            return [r.table_name for r in cur.tables(tableType="TABLE")]
        finally:
            cur.close()

    ok, tables = step("Liste des tables", list_tables, 120)
    if ok:
        say(f"    {len(tables)} table(s) : {', '.join(tables[:15])}{' …' if len(tables) > 15 else ''}")
        if tables:
            quote = (cnx.getinfo(hfsql.SQL_IDENTIFIER_QUOTE_CHAR) or "").strip()
            name = f"{quote}{tables[0]}{quote}"

            def structure():
                cur = cnx.cursor()
                try:
                    cur.execute(f"SELECT * FROM {name} WHERE 1=0")
                    return [f"{d[0]} ({d[1].__name__})" for d in cur.description]
                finally:
                    cur.close()

            def count():
                cur = cnx.cursor()
                try:
                    cur.execute(f"SELECT COUNT(*) FROM {name}")
                    return cur.fetchone()[0]
                finally:
                    cur.close()

            ok, cols = step(f"Lecture de la structure de « {tables[0]} »", structure, 120)
            if ok:
                say("    Colonnes : " + ", ".join(cols))
            ok, n = step(f"Comptage des lignes de « {tables[0]} »", count, 300)
            if ok:
                say(f"    {n} ligne(s)")
    try:
        cnx.close()
    except Exception:
        pass
    return finish(root)


def finish(root: str) -> int:
    path = os.path.join(root, "logs", "diagnostic-hfsql.txt")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(REPORT) + "\n")
    say(f"\nRapport enregistré : {path}")
    return 0


if __name__ == "__main__":
    code = main()
    os._exit(code)  # n'attend pas un éventuel pilote resté bloqué
