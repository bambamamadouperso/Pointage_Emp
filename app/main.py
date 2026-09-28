"""Point d'entrée : application web d'administration de la synchronisation MariaDB -> PostgreSQL."""
import html
import logging
from datetime import datetime, timezone
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import __version__, auth, scheduler
from .config import settings
from .database import SessionLocal, init_db
from .joblog import write_log
from .models import ROLES, User
from .routers import admin, connections, data, jobs, monitoring, rapports, suivi
from .web import LoginRequired, back_url, flash, redirect, render

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("app")


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    if settings.admin_password == "admin" or settings.secret_key == "change-me-in-production":
        logger.warning("ADMIN_PASSWORD et/ou SECRET_KEY ont leur valeur par défaut : changez-les en production.")
    if settings.scheduler_enabled:
        scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(title="Synchronisation MariaDB → PostgreSQL", lifespan=lifespan, docs_url=None, redoc_url=None)


@app.middleware("http")
async def _access_control(request: Request, call_next):
    """Connexion obligatoire et droits selon le rôle (lecteur < manager < admin)."""
    path = request.url.path
    if auth.PUBLIC_PATHS.match(path):
        return await call_next(request)
    user = request.session.get("user")
    role = await run_in_threadpool(auth.current_role, user) if user else None
    if user and role is None:  # compte supprimé ou désactivé
        request.session.clear()
    if role is None:
        return redirect("/login")
    request.session["role"] = role
    needed = auth.required_role(request.method, path)
    if not auth.has_role(role, needed):
        if request.method == "GET" and path == "/":
            return redirect(auth.home_for(role))
        if request.method == "GET":
            flash(request, "Accès refusé : cette page est réservée au rôle "
                           f"« {ROLES[needed]} » ou supérieur.", "err")
            return redirect(auth.home_for(role))
        if "text/html" in request.headers.get("accept", ""):
            flash(request, f"Action refusée : réservée au rôle « {ROLES[needed]} ».", "err")
            return redirect(back_url(request, auth.home_for(role)))
        return PlainTextResponse("Accès refusé.", status_code=403)
    response = await call_next(request)
    await run_in_threadpool(auth.auto_audit, request, response.status_code)
    return response


# Ajouté après : la session est donc disponible dans le contrôle d'accès ci-dessus.
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, same_site="lax", max_age=12 * 3600)
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")


@app.exception_handler(RequestValidationError)
async def _invalid_form(request: Request, exc: RequestValidationError):
    """Formulaire incomplet : message lisible au lieu d'une erreur JSON brute."""
    fields = ", ".join(sorted({str(e["loc"][-1]) for e in exc.errors() if e.get("loc")}))
    message = f"Formulaire incomplet ou invalide ({fields or 'champ inconnu'}) : vérifiez les champs et réessayez."
    if "text/html" not in request.headers.get("accept", ""):
        return JSONResponse({"error": message}, status_code=422)
    flash(request, message, "err")
    return redirect(back_url(request, "/"))


@app.exception_handler(LoginRequired)
async def _login_required(request: Request, _: LoginRequired):
    return redirect("/login")


@app.exception_handler(Exception)
async def _unexpected_error(request: Request, exc: Exception):
    """Erreur imprévue : trace complète dans le journal, page lisible (au lieu de « Internal Server Error »)."""
    when = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    logger.error("Erreur sur %s %s", request.method, request.url.path, exc_info=exc)
    summary = f"{exc.__class__.__name__} : {str(exc).strip()[:600]}"
    try:
        write_log("ERROR", f"Erreur de page {request.url.path} : {summary}")
    except Exception:
        pass
    logged_in = bool(request.scope.get("session", {}).get("user"))
    detail = (f"<p><b>Détail :</b></p><pre>{html.escape(summary)}</pre>" if logged_in else "")
    body = f"""<!doctype html><html lang="fr"><head><meta charset="utf-8"><title>Erreur</title>
<style>body{{font-family:Segoe UI,Arial,sans-serif;background:#f4f7f9;color:#1c2b36;margin:0;padding:40px 16px}}
main{{max-width:760px;margin:auto;background:#fff;border-radius:10px;padding:24px 28px;border-left:6px solid #b42318}}
pre{{white-space:pre-wrap;background:#f1f3f5;padding:12px;border-radius:6px}}a{{color:#0b4f6c}}</style></head>
<body><main><h1>La page n'a pas pu s'afficher</h1>
<p>Une erreur imprévue s'est produite sur <code>{html.escape(request.url.path)}</code> le {when}
(version {__version__}).</p>{detail}
<p>Le détail complet est dans <code>logs\\application.log</code> (chercher « Erreur sur » à cette heure).</p>
<p><a href="javascript:history.back()">← Retour</a> · <a href="/">Accueil</a></p></main></body></html>"""
    return HTMLResponse(body, status_code=500)


@app.get("/login")
def login_page(request: Request):
    """Page de connexion, toujours sans menu ; déjà connecté (compte valide) : directement à sa page d'accueil."""
    username = request.session.get("user")
    role = auth.current_role(username) if username else None
    if role:
        return redirect(auth.home_for(role))
    flashes = request.session.get("_flash", [])
    request.session.clear()  # session d'un compte supprimé, désactivé ou expiré
    if flashes:
        request.session["_flash"] = flashes
    return render(request, "login.html", user=None, role=None)


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    role = auth.authenticate(username, password)
    if role:
        request.session.clear()
        request.session["user"] = username.strip()
        request.session["role"] = role
        auth.audit(request, "Connexion", details=f"rôle {ROLES.get(role, role)}")
        return redirect(auth.home_for(role))
    write_log("WARNING", f"Échec de connexion au tableau de bord pour « {username} ».")
    auth.audit(request, "Échec de connexion", details=f"identifiant « {username[:100]} »", username="")
    flash(request, "Identifiants incorrects.", "err")
    return redirect("/login")


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return redirect("/login")


@app.get("/compte")
def account(request: Request):
    return render(request, "account.html", rescue=auth.is_rescue_admin(request.session.get("user", "")))


@app.post("/compte")
def change_password(request: Request, current: str = Form(...), new: str = Form(...), confirm: str = Form(...)):
    username = request.session.get("user", "")
    if auth.is_rescue_admin(username):
        flash(request, "Ce compte est défini dans le fichier .env (ADMIN_PASSWORD) : modifiez-le là.", "warn")
        return redirect("/compte")
    with SessionLocal() as db:
        user = db.query(User).filter(User.username == username).one_or_none()
        problem = auth.password_problem(new) or (None if new == confirm else "La confirmation ne correspond pas.")
        if user is None or not auth.verify_password(current, user.password_hash):
            problem = "Mot de passe actuel incorrect."
        if problem:
            flash(request, problem, "err")
            return redirect("/compte")
        user.password_hash = auth.hash_password(new)
        db.commit()
    auth.audit(request, "Mot de passe modifié", f"utilisateur {username}")
    flash(request, "Mot de passe modifié.", "ok")
    return redirect("/compte")


STARTED_AT = datetime.now(timezone.utc)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "version": __version__,
        "started_at": STARTED_AT.isoformat(),
        "scheduler": scheduler.scheduler.running,
    }


app.include_router(monitoring.router)
app.include_router(connections.router)
app.include_router(jobs.router)
app.include_router(data.router)
app.include_router(suivi.router)
app.include_router(rapports.router)
app.include_router(admin.router)
