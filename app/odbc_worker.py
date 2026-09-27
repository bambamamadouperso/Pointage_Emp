"""Processus isolé qui exécute le pilote ODBC HFSQL.

Le pilote HFSQL est du code natif : s'il plante (ou s'il n'accepte pas qu'une connexion soit utilisée
depuis plusieurs threads), il emporterait tout le serveur web. Il tourne donc dans un processus à part,
qui reçoit des commandes par un tube et renvoie les résultats. Toutes les commandes s'exécutent dans le
fil principal de ce processus : la connexion est toujours utilisée par le thread qui l'a ouverte.

Ce module ne doit rien importer de lourd : il est chargé au démarrage de chaque processus.
"""
import os
from typing import Any


class OdbcFailure(Exception):
    def __init__(self, message: str, visible=None):
        super().__init__(message)
        self.visible = visible


def _message(exc: Exception) -> str:
    args = getattr(exc, "args", ())
    return str(args[1] if len(args) > 1 else exc).strip()


def handle(state: dict, msg: dict, pyodbc) -> Any:
    """Exécute une commande. Partagé par le processus isolé et le mode « inline » (tests)."""
    op = msg["op"]
    if op == "connect":
        try:
            state["cnx"] = pyodbc.connect(msg["cs"], timeout=msg.get("login_timeout", 30), autocommit=True)
        except pyodbc.Error as exc:
            text = f"{getattr(exc, 'args', ('',))[0]} {_message(exc)}"
            visible = None
            if "IM002" in text:
                try:
                    visible = sorted(pyodbc.dataSources())
                except Exception:
                    visible = []
            raise OdbcFailure(_message(exc) if "IM002" not in _message(exc) else text, visible) from exc
        return None
    if op == "_crash":  # tests : simule un plantage natif du pilote
        os.abort()
    cnx = state.get("cnx")
    if cnx is None:
        raise OdbcFailure("connexion ODBC non ouverte")
    if op == "getinfo":
        return cnx.getinfo(msg["code"])
    if op in ("tables", "probe", "primary_keys", "describe", "scalar"):
        cur = cnx.cursor()
        try:
            if op == "tables":
                return [row.table_name for row in cur.tables(tableType="TABLE")]
            if op == "probe":
                cur.tables(tableType="TABLE").fetchone()
                return True
            if op == "primary_keys":
                try:
                    rows = sorted(cur.primaryKeys(table=msg["table"]), key=lambda r: r.key_seq or 0)
                    return [r.column_name for r in rows]
                except Exception:
                    return []  # fonction non gérée par le pilote
            if op == "describe":
                cur.execute(msg["sql"])
                return [(d[0], d[1], d[4], d[5]) for d in cur.description]
            cur.execute(msg["sql"], *msg.get("params", []))
            return cur.fetchone()[0]
        finally:
            cur.close()
    if op == "execute":
        if state.get("cur") is not None:
            state["cur"].close()
        state["cur"] = cnx.cursor()
        state["cur"].execute(msg["sql"], *msg.get("params", []))
        return None
    if op == "fetchmany":
        return [tuple(row) for row in state["cur"].fetchmany(msg["n"])]
    if op == "close_cursor":
        if state.get("cur") is not None:
            state["cur"].close()
            state["cur"] = None
        return None
    raise OdbcFailure(f"commande inconnue : {op}")


def serve(pipe) -> None:
    """Boucle du processus isolé."""
    import pyodbc

    state: dict = {}
    while True:
        try:
            msg = pipe.recv()
        except (EOFError, OSError):
            break
        if msg.get("op") == "quit":
            break
        try:
            pipe.send(("ok", handle(state, msg, pyodbc)))
        except Exception as exc:  # renvoyé au serveur web, qui l'affiche
            pipe.send(("err", _message(exc), getattr(exc, "visible", None)))
    try:
        if state.get("cnx") is not None:
            state["cnx"].close()
    except Exception:
        pass
