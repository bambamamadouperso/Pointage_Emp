"""Modèles de la base interne : connexions, jobs, tables, exécutions et logs."""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.engine import URL
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .crypto import decrypt
from .database import Base

SOURCE_KINDS = {"mariadb": "MariaDB / MySQL", "hfsql": "HFSQL Client/Serveur", "gsheet": "Google Sheets",
                "smartsheet": "Smartsheet"}
# Sources de type « feuille » : chaque onglet / feuille est lu comme une table.
SHEET_KINDS = ("gsheet", "smartsheet")
TARGET_KINDS = {"postgresql": "PostgreSQL"}
KIND_LABELS = {**SOURCE_KINDS, **TARGET_KINDS}
DEFAULT_PORTS = {"mariadb": 3306, "hfsql": 4900, "postgresql": 5432, "gsheet": 443, "smartsheet": 443}

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
    def is_smartsheet(self) -> bool:
        return self.kind == "smartsheet"

    @property
    def is_sheet(self) -> bool:
        """Google Sheets ou Smartsheet : onglets / feuilles lus comme des tables."""
        return self.kind in SHEET_KINDS

    @property
    def location(self) -> str:
        """Description courte affichée dans le tableau de bord."""
        if self.is_gsheet:
            return f"Google Sheets {self.database[:12]}…"
        if self.is_smartsheet:
            return f"Smartsheet {self.database[:24]}"
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


# --------------------------------------------------------------------------- utilisateurs, audit, pointage

ROLES = {"admin": "Administrateur", "manager": "Manager", "lecteur": "Lecteur"}


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(100), unique=True)
    full_name: Mapped[str] = mapped_column(String(200), default="")
    role: Mapped[str] = mapped_column(String(20), default="lecteur")
    password_hash: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # Rattachement à un employé (matricule) et périmètre du suivi : « tous » ou « equipe » (sa hiérarchie).
    emp_matricule: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    scope: Mapped[Optional[str]] = mapped_column(String(20), nullable=True, default="tous")
    # Agent RH habilité à saisir les arrêts maladie (validés d'office) et à valider les étapes « RH ».
    sick_leave_hr: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, default=False)
    # Adresse e-mail du compte (notifications) ; à défaut, celle de la fiche employé (matricule).
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    @property
    def role_label(self) -> str:
        return ROLES.get(self.role, self.role)

    @property
    def team_only(self) -> bool:
        return self.scope == "equipe" and self.role != "admin"


class AuditEntry(Base):
    """Journal d'audit : qui a modifié quoi, et quand."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    username: Mapped[str] = mapped_column(String(100), default="")
    action: Mapped[str] = mapped_column(String(200))
    target: Mapped[str] = mapped_column(String(300), default="")
    details: Mapped[str] = mapped_column(Text, default="")
    ip: Mapped[str] = mapped_column(String(64), default="")


class PointageConfig(Base):
    """Correspondance entre le module de pointage et les tables PostgreSQL (une seule ligne)."""

    __tablename__ = "pointage_config"

    id: Mapped[int] = mapped_column(primary_key=True)
    conn_id: Mapped[Optional[int]] = mapped_column(ForeignKey("connections.id", ondelete="SET NULL"), nullable=True)
    data: Mapped[str] = mapped_column(Text, default="{}")
    installed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # Version des objets PostgreSQL installés (réinstallés automatiquement après une mise à jour).
    sql_version: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    conn: Mapped[Optional[Connection]] = relationship()


class MailSettings(Base):
    """Mails de confirmation de badge : serveur SMTP, mode (test / production), modèle (une seule ligne)."""

    __tablename__ = "mail_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # « test » : tous les mails partent vers test_recipients ; « production » : vers les employés abonnés.
    mode: Mapped[str] = mapped_column(String(20), default="test")
    test_recipients: Mapped[str] = mapped_column(Text, default="")
    # Seuls les pointages postérieurs à cette date (heure locale) sont notifiés : jamais l'historique.
    since: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    smtp_host: Mapped[str] = mapped_column(String(255), default="")
    smtp_port: Mapped[int] = mapped_column(Integer, default=587)
    smtp_security: Mapped[str] = mapped_column(String(20), default="starttls")  # starttls, ssl, none
    smtp_user: Mapped[str] = mapped_column(String(255), default="")
    smtp_password_enc: Mapped[str] = mapped_column(Text, default="")
    from_email: Mapped[str] = mapped_column(String(255), default="")
    from_name: Mapped[str] = mapped_column(String(255), default="Pointage")
    reply_to: Mapped[str] = mapped_column(String(255), default="")
    company: Mapped[str] = mapped_column(String(255), default="")
    max_per_run: Mapped[int] = mapped_column(Integer, default=200)
    subject: Mapped[str] = mapped_column(Text, default="")
    html: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_run_summary: Mapped[str] = mapped_column(Text, default="")


class MailSubscriber(Base):
    """Employé abonné aux mails de confirmation de badge (adresse de la fiche, ou adresse saisie ici)."""

    __tablename__ = "mail_subscribers"

    id: Mapped[int] = mapped_column(primary_key=True)
    emp_key: Mapped[str] = mapped_column(String(100), unique=True)
    matricule: Mapped[str] = mapped_column(String(100), default="")
    name: Mapped[str] = mapped_column(String(255), default="")
    email: Mapped[str] = mapped_column(String(255), default="")  # vide : adresse de la fiche employé
    added_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    added_by: Mapped[str] = mapped_column(String(100), default="")


class MailLog(Base):
    """Un pointage notifié (ou non) : empêche tout second envoi pour le même pointage."""

    __tablename__ = "mail_log"
    __table_args__ = (UniqueConstraint("emp_key", "punch_at", name="uq_mail_log_punch"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    emp_key: Mapped[str] = mapped_column(String(100), index=True)
    matricule: Mapped[str] = mapped_column(String(100), default="")
    name: Mapped[str] = mapped_column(String(255), default="")
    punch_at: Mapped[datetime] = mapped_column(DateTime)
    mode: Mapped[str] = mapped_column(String(20), default="test")
    recipient: Mapped[str] = mapped_column(Text, default="")        # adresse(s) réellement utilisée(s)
    intended: Mapped[str] = mapped_column(String(255), default="")  # adresse de l'employé
    status: Mapped[str] = mapped_column(String(20), default="sent")  # sent, failed, skipped
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    error: Mapped[str] = mapped_column(Text, default="")


SICK_STATUS = {"en_attente": "En attente de validation", "valide": "Validé", "refuse": "Refusé", "annule": "Annulé"}


class SickLeave(Base):
    """Arrêt maladie déclaré par l'employé (circuit de validation) ou saisi par un agent RH (validé d'office)."""

    __tablename__ = "sick_leaves"

    id: Mapped[int] = mapped_column(primary_key=True)
    emp_key: Mapped[str] = mapped_column(String(100), index=True)
    matricule: Mapped[str] = mapped_column(String(100), default="")
    name: Mapped[str] = mapped_column(String(255), default="")
    start_date: Mapped[datetime] = mapped_column(DateTime)
    end_date: Mapped[datetime] = mapped_column(DateTime)
    comment: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(20), default="employe")    # employe, rh
    status: Mapped[str] = mapped_column(String(20), default="en_attente")  # voir SICK_STATUS
    step: Mapped[int] = mapped_column(Integer, default=0)                  # étape du circuit en cours (0 = première)
    workflow: Mapped[str] = mapped_column(Text, default="[]")              # circuit figé à la déclaration (JSON)
    manager_matricule: Mapped[str] = mapped_column(String(100), default="")  # responsable N+1 au moment de la déclaration
    manager_name: Mapped[str] = mapped_column(String(255), default="")
    file_name: Mapped[str] = mapped_column(String(255), default="")        # nom d'origine du justificatif
    file_path: Mapped[str] = mapped_column(String(255), default="")        # nom du fichier stocké
    file_type: Mapped[str] = mapped_column(String(100), default="")
    created_by: Mapped[str] = mapped_column(String(100), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    @property
    def status_label(self) -> str:
        return SICK_STATUS.get(self.status, self.status)

    @property
    def days(self) -> int:
        return (self.end_date.date() - self.start_date.date()).days + 1


class SickLeaveAction(Base):
    """Historique d'un arrêt maladie : déclaration, validations, refus, annulation."""

    __tablename__ = "sick_leave_actions"

    id: Mapped[int] = mapped_column(primary_key=True)
    leave_id: Mapped[int] = mapped_column(ForeignKey("sick_leaves.id", ondelete="CASCADE"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    actor: Mapped[str] = mapped_column(String(100), default="")
    action: Mapped[str] = mapped_column(String(30), default="")  # declare, valide, refuse, annule, saisi_rh
    step_label: Mapped[str] = mapped_column(String(255), default="")
    comment: Mapped[str] = mapped_column(Text, default="")


class SickLeaveSettings(Base):
    """Circuit de validation des arrêts déclarés par les employés (étapes ordonnées, JSON)."""

    __tablename__ = "sick_leave_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    workflow: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    updated_by: Mapped[str] = mapped_column(String(100), default="")
    # Mails aux valideurs (arrêt à valider) et à l'employé (décision), via le serveur SMTP des mails de badge.
    notify: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, default=False)


DIGEST_KINDS = {"quotidien": "Quotidien", "hebdomadaire": "Hebdomadaire"}
DIGEST_FREQUENCIES = {"quotidien": "Quotidien", "hebdomadaire": "Hebdomadaire", "les_deux": "Quotidien et hebdomadaire"}


class DigestSettings(Base):
    """Résumés des pointages envoyés aux responsables (équipe N-1) : planification et contenu (une seule ligne)."""

    __tablename__ = "digest_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    daily_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    daily_time: Mapped[str] = mapped_column(String(5), default="07:30")      # résumé de la veille
    weekly_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    weekly_day: Mapped[int] = mapped_column(Integer, default=1)              # 1 = lundi : résumé de la semaine passée
    weekly_time: Mapped[str] = mapped_column(String(5), default="07:30")
    scope: Mapped[str] = mapped_column(String(20), default="directs")        # directs (N-1) ou equipe (toute la hiérarchie)
    skip_empty: Mapped[bool] = mapped_column(Boolean, default=True)          # pas de mail si personne n'était attendu
    app_url: Mapped[str] = mapped_column(String(255), default="")           # lien « Ouvrir le suivi » dans les mails
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    updated_by: Mapped[str] = mapped_column(String(100), default="")
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_run_summary: Mapped[str] = mapped_column(Text, default="")


class DigestSubscriber(Base):
    """Responsable qui reçoit le résumé de son équipe (adresse de sa fiche employé, ou adresse saisie ici)."""

    __tablename__ = "digest_subscribers"

    id: Mapped[int] = mapped_column(primary_key=True)
    manager_key: Mapped[str] = mapped_column(String(100), unique=True)
    matricule: Mapped[str] = mapped_column(String(100), default="")
    name: Mapped[str] = mapped_column(String(255), default="")
    email: Mapped[str] = mapped_column(String(255), default="")
    frequency: Mapped[str] = mapped_column(String(20), default="hebdomadaire")
    added_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    added_by: Mapped[str] = mapped_column(String(100), default="")


class DigestLog(Base):
    """Un résumé envoyé (ou en échec) : un seul envoi par responsable, type et période."""

    __tablename__ = "digest_log"
    __table_args__ = (UniqueConstraint("kind", "period_start", "manager_key", name="uq_digest_period"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    kind: Mapped[str] = mapped_column(String(20))
    period_start: Mapped[datetime] = mapped_column(DateTime)
    period_end: Mapped[datetime] = mapped_column(DateTime)
    manager_key: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(255), default="")
    mode: Mapped[str] = mapped_column(String(20), default="test")
    recipient: Mapped[str] = mapped_column(Text, default="")
    intended: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(20), default="sent")  # sent, failed, skipped
    error: Mapped[str] = mapped_column(Text, default="")
    attempts: Mapped[int] = mapped_column(Integer, default=1)
