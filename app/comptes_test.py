"""Crée (ou réinitialise) deux comptes de test : un utilisateur normal et un utilisateur RH.

    python -m app.comptes_test                      # mots de passe générés et affichés
    python -m app.comptes_test --mdp-employe XXX --mdp-rh YYY [--matricule 590394]

- test.employe : rôle « Lecteur » (utilisateur normal) rattaché à un employé qui encadre une équipe (des N-1) :
  sans --matricule, le responsable qui a le plus de collaborateurs directs est choisi automatiquement. Son suivi est
  limité à son équipe ; il voit « Mes retards », justifie ses retards, et approuve ceux de ses N-1 (étape
  « Responsable N+1 »), comme les activités extérieures et les arrêts de son équipe.
- test.rh : rôle « RH » et agent RH habilité (valide les étapes RH, saisies RH validées d'office).

Relancer la commande remet les mots de passe indiqués et réactive les comptes. Supprimez-les ensuite depuis
Administration → Utilisateurs.
"""
import argparse
import secrets
import string
import sys

ACCOUNTS = (("test.employe", "Compte de test — utilisateur", "lecteur", False),
            ("test.rh", "Compte de test — RH", "rh", True))


def _password() -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(12)) + "!7"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Crée les comptes de test test.employe et test.rh.")
    parser.add_argument("--mdp-employe", default="", help="mot de passe de test.employe (sinon généré)")
    parser.add_argument("--mdp-rh", default="", help="mot de passe de test.rh (sinon généré)")
    parser.add_argument("--matricule", default="", help="matricule de l'employé (avec des N-1) rattaché à test.employe ; "
                                                        "sinon choisi automatiquement")
    args = parser.parse_args(argv)

    from .auth import hash_password
    from .database import SessionLocal, init_db
    from .models import User

    init_db()
    passwords = {"test.employe": args.mdp_employe or _password(), "test.rh": args.mdp_rh or _password()}
    for pwd in passwords.values():
        if len(pwd) < 8:
            print("Mot de passe trop court (8 caractères au moins).", file=sys.stderr)
            return 1
    with SessionLocal() as db:
        matricule, info = _employee_with_team(db, args.matricule.strip())
        for username, full_name, role, hr in ACCOUNTS:
            user = db.query(User).filter(User.username == username).one_or_none()
            if user is None:
                user = User(username=username)
                db.add(user)
            user.full_name, user.role, user.active, user.sick_leave_hr = full_name, role, True, hr
            user.password_hash, user.must_change_password = hash_password(passwords[username]), False
            user.scope = "tous"
            if username == "test.employe":
                user.scope = "equipe" if matricule else "tous"
                if matricule:
                    user.emp_matricule = matricule
        db.commit()
    print("Comptes de test prêts :")
    for username, _, role, _ in ACCOUNTS:
        print(f"  {username:<14} rôle {role:<8} mot de passe : {passwords[username]}")
    for line in info:
        print(line)
    return 0


def _employee_with_team(db, matricule: str) -> tuple[str, list[str]]:
    """Matricule de l'employé simulé par test.employe (un responsable qui a des N-1) et lignes d'information."""
    from . import pointage
    from .routers.suivi import load_config
    from .sync import make_engine

    cfg, m = load_config(db)
    if m is None:
        return matricule, ["  (module de pointage non configuré : test.employe n'est rattaché à aucun employé)"]
    engine = make_engine(cfg.conn, **pointage.WEB_LIMITS)
    try:
        team = {key: (label, n) for key, label, n in pointage.managers(engine, m)}
        if matricule:
            found = pointage.resolve_employee(engine, m, matricule)
            if found is None:
                return matricule, [f"  ATTENTION : matricule {matricule} introuvable dans la liste des employés."]
            key = found[0]
        elif team:
            key = max(team, key=lambda k: (team[k][1], team[k][0]))
            matricule = pointage.employee_matricule(engine, m, key) or key
        else:
            return "", ["  ATTENTION : aucune hiérarchie configurée (aucun responsable) : test.employe voit tout le personnel."]
        label, n = team.get(key, (pointage.resolve_employee(engine, m, key)[1], 0))
        info = [f"test.employe simule : {label} — {n} collaborateur(s) direct(s) (N-1)."]
        if not n:
            info.append("  ATTENTION : cet employé n'encadre personne ; choisissez un responsable avec --matricule.")
        boss = pointage.manager_of(engine, m, key)
        if boss:
            info.append(f"  Son N+1 : {boss[1]} ({boss[0]}) — valide ses justifications s'il a un compte, sinon test.rh.")
        return matricule, info
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
