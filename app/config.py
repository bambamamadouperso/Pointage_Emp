"""Configuration de l'application, lue depuis les variables d'environnement."""
import os
from dataclasses import dataclass, field


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    # Base interne qui stocke les connexions, les jobs, les exécutions et les logs.
    app_db_url: str = field(default_factory=lambda: os.getenv("APP_DB_URL", "sqlite:///./data/app.db"))
    # Clé de signature des sessions du tableau de bord.
    secret_key: str = field(default_factory=lambda: os.getenv("SECRET_KEY", "change-me-in-production"))
    # Clé Fernet pour chiffrer les mots de passe des bases (dérivée de SECRET_KEY si absente).
    encryption_key: str = field(default_factory=lambda: os.getenv("ENCRYPTION_KEY", ""))
    admin_username: str = field(default_factory=lambda: os.getenv("ADMIN_USERNAME", "admin"))
    admin_password: str = field(default_factory=lambda: os.getenv("ADMIN_PASSWORD", "admin"))
    # Niveau minimum des logs enregistrés en base (DEBUG pour tracer chaque lot).
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO").upper())
    # Nombre de lignes lues/écrites par lot.
    batch_size: int = field(default_factory=lambda: _int("BATCH_SIZE", 5000))
    # Durée de conservation des logs et de l'historique des exécutions.
    log_retention_days: int = field(default_factory=lambda: _int("LOG_RETENTION_DAYS", 30))
    # Nombre maximum de jobs de synchronisation exécutés en parallèle.
    max_workers: int = field(default_factory=lambda: _int("SCHEDULER_MAX_WORKERS", 4))
    # Durée maximale d'une exécution (minutes) : au-delà, elle est arrêtée automatiquement (0 = sans limite).
    max_run_minutes: int = field(default_factory=lambda: _int("MAX_RUN_MINUTES", 360))
    # Fuseau horaire d'affichage des dates dans le tableau de bord.
    timezone: str = field(default_factory=lambda: os.getenv("APP_TIMEZONE", "UTC"))
    # Désactive le planificateur (utile pour les tests).
    scheduler_enabled: bool = field(default_factory=lambda: os.getenv("SCHEDULER_ENABLED", "1") != "0")


settings = Settings()
