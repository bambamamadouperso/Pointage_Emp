"""Point d'entrée : application web d'administration de la synchronisation MariaDB -> PostgreSQL."""
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Form, Request
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import scheduler
from .config import settings
from .database import init_db
from .joblog import write_log
from .routers import connections, data, jobs, monitoring
from .web import LoginRequired, check_credentials, flash, redirect, render

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
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, same_site="lax", max_age=12 * 3600)
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")


@app.exception_handler(LoginRequired)
async def _login_required(request: Request, _: LoginRequired):
    return redirect("/login")


@app.get("/login")
def login_page(request: Request):
    return render(request, "login.html")


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    if check_credentials(username, password):
        request.session["user"] = username
        return redirect("/")
    write_log("WARNING", f"Échec de connexion au tableau de bord pour « {username} ».")
    flash(request, "Identifiants incorrects.", "err")
    return redirect("/login")


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return redirect("/login")


@app.get("/health")
def health():
    return {"status": "ok", "scheduler": scheduler.scheduler.running}


app.include_router(monitoring.router)
app.include_router(connections.router)
app.include_router(jobs.router)
app.include_router(data.router)
