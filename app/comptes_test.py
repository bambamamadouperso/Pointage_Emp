"""Crée (ou réinitialise) deux comptes de test : un utilisateur normal et un utilisateur RH.

    python -m app.comptes_test                      # mots de passe générés et affichés
    python -m app.comptes_test --mdp-employe XXX --mdp-rh YYY [--matricule 590394]

- test.employe : rôle « Lecteur » (utilisateur normal) ; avec --matricule, rattaché à cet employé (il voit alors
  « Mes retards », peut justifier un retard, déclarer une activité…).
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
    parser.add_argument("--matricule", default="", help="matricule de l'employé rattaché à test.employe")
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
        for username, full_name, role, hr in ACCOUNTS:
            user = db.query(User).filter(User.username == username).one_or_none()
            if user is None:
                user = User(username=username)
                db.add(user)
            user.full_name, user.role, user.active, user.sick_leave_hr = full_name, role, True, hr
            user.password_hash, user.must_change_password = hash_password(passwords[username]), False
            user.scope = "tous"
            if username == "test.employe" and args.matricule:
                user.emp_matricule = args.matricule.strip()
        db.commit()
    print("Comptes de test prêts :")
    for username, _, role, _ in ACCOUNTS:
        print(f"  {username:<14} rôle {role:<8} mot de passe : {passwords[username]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
