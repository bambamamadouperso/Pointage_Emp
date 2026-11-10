"""Administration → Apparence : fond de la page de connexion."""
from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from .. import auth
from ..database import get_db
from ..models import LOGIN_BACKGROUNDS, AppearanceSettings, utcnow
from ..web import flash, redirect, render, require_login

router = APIRouter(prefix="/admin/apparence", dependencies=[Depends(require_login)])


def get_settings(db: Session) -> AppearanceSettings:
    row = db.query(AppearanceSettings).order_by(AppearanceSettings.id).first()
    if row is None:
        row = AppearanceSettings(login_background="vagues")
        db.add(row)
        db.commit()
    return row


@router.get("")
def page(request: Request, db: Session = Depends(get_db)):
    return render(request, "admin/apparence.html", s=get_settings(db), backgrounds=LOGIN_BACKGROUNDS)


@router.post("")
def save(request: Request, fond: str = Form(""), db: Session = Depends(get_db)):
    if fond not in LOGIN_BACKGROUNDS:
        flash(request, "Choisissez un fond dans la liste.", "err")
        return redirect("/admin/apparence")
    s = get_settings(db)
    s.login_background, s.updated_at, s.updated_by = fond, utcnow(), request.session.get("user", "")
    db.commit()
    auth.audit(request, "Apparence : fond de la page de connexion", LOGIN_BACKGROUNDS[fond][0])
    flash(request, f"Fond de la page de connexion : « {LOGIN_BACKGROUNDS[fond][0]} ».", "ok")
    return redirect("/admin/apparence")
