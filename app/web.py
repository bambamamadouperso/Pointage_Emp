"""Outils communs aux pages du tableau de bord : templates, messages flash, authentification."""
import os
import secrets
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlencode, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from .config import settings

templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

try:
    _tz = ZoneInfo(settings.timezone)
except ZoneInfoNotFoundError:
    _tz = timezone.utc


def fmt_dt(value: Optional[datetime], with_seconds: bool = True) -> str:
    if value is None:
        return "—"
    local = value.replace(tzinfo=timezone.utc).astimezone(_tz)
    return local.strftime("%d/%m/%Y %H:%M:%S" if with_seconds else "%d/%m/%Y %H:%M")


def fmt_int(value: Optional[int]) -> str:
    if value is None:
        return "—"
    return f"{value:,}".replace(",", " ")


STATUS_LABELS = {
    "success": ("Succès", "ok"),
    "partial": ("Partiel", "warn"),
    "error": ("Erreur", "err"),
    "running": ("En cours", "run"),
    "cancelled": ("Arrêté", "warn"),
}


def status_badge(status: Optional[str]) -> str:
    from markupsafe import Markup, escape

    if not status:
        return Markup('<span class="badge">Jamais exécuté</span>')
    label, cls = STATUS_LABELS.get(status, (status, ""))
    return Markup(f'<span class="badge {cls}">{escape(label)}</span>')


templates.env.filters["dt"] = fmt_dt
templates.env.filters["num"] = fmt_int
templates.env.globals["status_badge"] = status_badge
templates.env.globals["tz_name"] = settings.timezone
templates.env.globals["app_version"] = __import__("app").__version__


def flash(request: Request, message: str, category: str = "info") -> None:
    request.session.setdefault("_flash", []).append([category, message])


def pop_flashes(request: Request) -> list:
    return request.session.pop("_flash", [])


templates.env.globals["pop_flashes"] = pop_flashes


def page_url(request: Request, page: int) -> str:
    params = [(k, v) for k, v in request.query_params.multi_items() if k != "page" and v != ""]
    return "?" + urlencode(params + [("page", page)])


templates.env.globals["page_url"] = page_url


def sort_url(request: Request, column: str, direction: str) -> str:
    """Même page, triée sur une colonne (retour à la première page)."""
    params = [(k, v) for k, v in request.query_params.multi_items() if k not in ("sort", "dir", "page")]
    return "?" + urlencode(params + [("sort", column), ("dir", direction)])


templates.env.globals["sort_url"] = sort_url


def render(request: Request, name: str, **context):
    context.setdefault("user", request.session.get("user"))
    context.setdefault("role", request.session.get("role"))
    return templates.TemplateResponse(request, name, context)


def _can(role, minimum: str) -> bool:
    from .auth import has_role

    return has_role(role, minimum)


templates.env.globals["can"] = _can

_JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]


def jour_fr(value, short: bool = False) -> str:
    """« Lundi 06/10/2026 » (ou « lun. 06/10/2026 »)."""
    name = _JOURS[value.weekday()]
    return f"{name[:3]}. {value:%d/%m/%Y}" if short else f"{name.capitalize()} {value:%d/%m/%Y}"


def replace_param(params, key: str, value: str) -> str:
    """Paramètres de l'URL actuelle avec une valeur remplacée (et retour à la page 1)."""
    items = [(k, v) for k, v in params.multi_items() if k not in (key, "page", "du", "au")]
    return urlencode(items + [(key, value)])


templates.env.filters["jour_fr"] = jour_fr
templates.env.filters["replace_param"] = replace_param
templates.env.globals["timedelta"] = __import__("datetime").timedelta


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def back_url(request: Request, default: str) -> str:
    """Page précédente (chemin local uniquement, pour éviter les redirections externes)."""
    path = urlparse(request.headers.get("referer") or "").path
    return path if path.startswith("/") and not path.startswith("//") else default


def check_credentials(username: str, password: str) -> bool:
    ok_user = secrets.compare_digest(username.encode(), settings.admin_username.encode())
    ok_pass = secrets.compare_digest(password.encode(), settings.admin_password.encode())
    return ok_user and ok_pass


class LoginRequired(Exception):
    pass


def require_login(request: Request) -> str:
    user = request.session.get("user")
    if not user:
        raise LoginRequired()
    return user
