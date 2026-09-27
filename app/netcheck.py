"""Outils réseau : vérification rapide d'un port et exécution avec délai maximal."""
import queue
import socket
import threading
from typing import Any, Callable


class NetError(Exception):
    pass


def check_port(host: str, port: int, timeout: float = 5.0) -> None:
    """Vérifie qu'un port TCP répond, avant de laisser un pilote tenter (parfois longuement) la connexion."""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return
    except socket.gaierror as exc:
        raise NetError(f"Nom d'hôte « {host} » inconnu : vérifiez l'adresse du serveur.") from exc
    except socket.timeout as exc:
        raise NetError(
            f"Le serveur {host} ne répond pas sur le port {port} (délai de {timeout:.0f} s dépassé). "
            f"Un pare-feu bloque probablement la connexion entre ce serveur et {host}:{port}, "
            f"ou l'adresse / le port sont incorrects."
        ) from exc
    except OSError as exc:
        raise NetError(
            f"Connexion refusée par {host}:{port} : le service n'écoute pas sur ce port "
            f"(serveur arrêté, mauvais port) ou un pare-feu la rejette. ({exc.strerror or exc})"
        ) from exc


def call_with_timeout(fn: Callable[[], Any], seconds: float, message: str) -> Any:
    """Exécute fn dans un thread ; lève NetError si elle ne répond pas à temps (le thread est abandonné)."""
    result: "queue.Queue[tuple[bool, Any]]" = queue.Queue(maxsize=1)

    def target():
        try:
            result.put((True, fn()))
        except BaseException as exc:  # transmis à l'appelant
            result.put((False, exc))

    threading.Thread(target=target, daemon=True).start()
    try:
        ok, value = result.get(timeout=seconds)
    except queue.Empty:
        raise NetError(message) from None
    if ok:
        return value
    raise value
