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


def flash(request: Request, message: str, category: str = "info") -> None:
    request.session.setdefault("_flash", []).append([category, message])


def pop_flashes(request: Request) -> list:
    return request.session.pop("_flash", [])


templates.env.globals["pop_flashes"] = pop_flashes


def page_url(request: Request, page: int) -> str:
    params = [(k, v) for k, v in request.query_params.multi_items() if k != "page" and v != ""]
    return "?" + urlencode(params + [("page", page)])


templates.env.globals["page_url"] = page_url


def render(request: Request, name: str, **context):
    context.setdefault("user", request.session.get("user"))
    return templates.TemplateResponse(request, name, context)


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
