"""Modèles de la base interne : connexions, jobs, tables, exécutions et logs."""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.engine import URL
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .crypto import decrypt
from .database import Base

SOURCE_KINDS = {"mariadb": "MariaDB / MySQL", "hfsql": "HFSQL Client/Serveur", "gsheet": "Google Sheets"}
TARGET_KINDS = {"postgresql": "PostgreSQL"}
KIND_LABELS = {**SOURCE_KINDS, **TARGET_KINDS}
DEFAULT_PORTS = {"mariadb": 3306, "hfsql": 4900, "postgresql": 5432, "gsheet": 443}

MODE_FULL = "full"
MODE_INCREMENTAL = "incremental"
MODE_LABELS = {
    MODE_FULL: "Complet (vidage + rechargement)",
    MODE_INCREMENTAL: "Incrémental (colonne de suivi)",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Connection(Base):
    __tablename__ = "connections"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    kind: Mapped[str] = mapped_column(String(20))
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer)
    database: Mapped[str] = mapped_column(String(255))
    username: Mapped[str] = mapped_column(String(255))
    password_enc: Mapped[str] = mapped_column(Text, default="")
    # Paramètres supplémentaires (HFSQL : nom du pilote ODBC, options de chaîne de connexion).
    options: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    @property
    def kind_label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind)

    # Pour Google Sheets : database = identifiant du classeur, username = mode d'accès,
    # password_enc = clé JSON du compte de service (chiffrée).
    @property
    def is_odbc(self) -> bool:
        return self.kind == "hfsql"

    @property
    def is_gsheet(self) -> bool:
        return self.kind == "gsheet"

    @property
    def location(self) -> str:
        """Description courte affichée dans le tableau de bord."""
        if self.is_gsheet:
            return f"Google Sheets {self.database[:12]}…"
        return f"{self.host}/{self.database}"

    @property
    def sheet_url(self) -> str:
        return f"https://docs.google.com/spreadsheets/d/{self.database}" if self.is_gsheet else ""

    def sqlalchemy_url(self) -> URL:
        if self.kind == "mariadb":
            return URL.create(
                "mysql+pymysql",
                username=self.username,
                password=decrypt(self.password_enc),
                host=self.host,
                port=self.port,
                database=self.database,
                query={"charset": "utf8mb4"},
            )
        if self.kind == "postgresql":
            return URL.create(
                "postgresql+psycopg",
                username=self.username,
                password=decrypt(self.password_enc),
                host=self.host,
                port=self.port,
                database=self.database,
            )
        raise ValueError(f"Type de base inconnu : {self.kind}")


class SyncJob(Base):
    __tablename__ = "sync_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("connections.id"))
    target_id: Mapped[int] = mapped_column(ForeignKey("connections.id"))
    target_schema: Mapped[str] = mapped_column(String(100), default="public")
    interval_seconds: Mapped[int] = mapped_column(Integer, default=3600)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_status: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    # Protections (minutes, 0 = sans limite, vide = valeur par défaut MAX_RUN_MINUTES / LOCK_WAIT_MINUTES).
    max_run_minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    lock_wait_minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Relances après un arrêt automatique (vide = RETRY_MAX / RETRY_DELAY_MINUTES, 0 relance = aucune).
    retry_max: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    retry_delay_minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    retry_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=0)
    # Raison de la suspension automatique (relances épuisées) ; effacée à la réactivation.
    suspended_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    source: Mapped[Connection] = relationship(foreign_keys=[source_id])
    target: Mapped[Connection] = relationship(foreign_keys=[target_id])
    tables: Mapped[list["TableMapping"]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="TableMapping.id"
    )

    @property
    def effective_max_run_minutes(self) -> int:
        from .config import settings

        return settings.max_run_minutes if self.max_run_minutes is None else self.max_run_minutes

    @property
    def effective_lock_wait_minutes(self) -> int:
        from .config import settings

        return settings.lock_wait_minutes if self.lock_wait_minutes is None else self.lock_wait_minutes

    @property
    def effective_retry_max(self) -> int:
        from .config import settings

        return settings.retry_max if self.retry_max is None else self.retry_max

    @property
    def effective_retry_delay_minutes(self) -> int:
        from .config import settings

        return settings.retry_delay_minutes if self.retry_delay_minutes is None else self.retry_delay_minutes

    @property
    def interval_label(self) -> str:
        s = self.interval_seconds
        if s % 86400 == 0:
            return f"{s // 86400} j"
        if s % 3600 == 0:
            return f"{s // 3600} h"
        if s % 60 == 0:
            return f"{s // 60} min"
        return f"{s} s"


class TableMapping(Base):
    __tablename__ = "table_mappings"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("sync_jobs.id", ondelete="CASCADE"))
    source_table: Mapped[str] = mapped_column(String(255))
    target_table: Mapped[str] = mapped_column(String(255))
    mode: Mapped[str] = mapped_column(String(20), default=MODE_FULL)
    incremental_column: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # Colonnes clés pour l'upsert (séparées par des virgules). Vide = clé primaire source.
    key_columns: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    # Dernière valeur transférée de la colonne incrémentale (encodée en JSON).
    last_value: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_sync_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_rows: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    job: Mapped[SyncJob] = relationship(back_populates="tables")

    @property
    def mode_label(self) -> str:
        return MODE_LABELS.get(self.mode, self.mode)

    @property
    def last_value_display(self) -> str:
        from .watermark import decode

        value = decode(self.last_value)
        return "" if value is None else str(value)


class JobRun(Base):
    __tablename__ = "job_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("sync_jobs.id", ondelete="CASCADE"), index=True)
    trigger: Mapped[str] = mapped_column(String(20), default="schedule")
    status: Mapped[str] = mapped_column(String(20), default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    rows_read: Mapped[int] = mapped_column(Integer, default=0)
    rows_written: Mapped[int] = mapped_column(Integer, default=0)
    tables_ok: Mapped[int] = mapped_column(Integer, default=0)
    tables_failed: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    job: Mapped[SyncJob] = relationship()

    @property
    def duration(self) -> str:
        if not self.finished_at:
            return "—"
        seconds = (self.finished_at - self.started_at).total_seconds()
        if seconds < 60:
            return f"{seconds:.1f} s"
        return f"{int(seconds // 60)} min {int(seconds % 60)} s"


class LogEntry(Base):
    __tablename__ = "logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    level: Mapped[str] = mapped_column(String(10), index=True)
    job_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("sync_jobs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    run_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("job_runs.id", ondelete="CASCADE"), nullable=True, index=True
    )
    table_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    message: Mapped[str] = mapped_column(Text)

    job: Mapped[Optional[SyncJob]] = relationship()
