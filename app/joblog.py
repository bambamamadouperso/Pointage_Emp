"""Écriture des logs applicatifs dans la base interne (visibles dans le tableau de bord)."""
import logging
from typing import Optional

from .config import settings
from .database import SessionLocal
from .models import LogEntry

logger = logging.getLogger("sync")

LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


def write_log(
    level: str,
    message: str,
    job_id: Optional[int] = None,
    run_id: Optional[int] = None,
    table_name: Optional[str] = None,
) -> None:
    """Enregistre un log dans sa propre transaction pour qu'il soit visible immédiatement."""
    level = level.upper() if level.upper() in LEVELS else "INFO"
    prefix = f"[job={job_id} run={run_id}{' table=' + table_name if table_name else ''}] "
    logger.log(getattr(logging, level, logging.INFO), prefix + message)
    if LEVELS.index(level) < LEVELS.index(settings.log_level if settings.log_level in LEVELS else "INFO"):
        return
    db = SessionLocal()
    try:
        db.add(LogEntry(level=level, message=message, job_id=job_id, run_id=run_id, table_name=table_name))
        db.commit()
    except Exception:  # un log ne doit jamais faire échouer une synchronisation
        logger.exception("Impossible d'écrire le log en base")
        db.rollback()
    finally:
        db.close()


class RunLogger:
    """Logger lié à une exécution de job."""

    def __init__(self, job_id: int, run_id: Optional[int]):
        self.job_id = job_id
        self.run_id = run_id

    def __call__(self, level: str, message: str, table_name: Optional[str] = None) -> None:
        write_log(level, message, job_id=self.job_id, run_id=self.run_id, table_name=table_name)

    def info(self, message: str, table_name: Optional[str] = None) -> None:
        self("INFO", message, table_name)

    def warning(self, message: str, table_name: Optional[str] = None) -> None:
        self("WARNING", message, table_name)

    def error(self, message: str, table_name: Optional[str] = None) -> None:
        self("ERROR", message, table_name)

    def debug(self, message: str, table_name: Optional[str] = None) -> None:
        self("DEBUG", message, table_name)
