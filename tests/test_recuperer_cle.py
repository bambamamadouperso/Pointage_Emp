from app import recuperer_cle as rc


def _token(kind, value, secret="motdepasse"):
    return rc.fernet_for(kind, value).encrypt(secret.encode()).decode()


def test_old_key_found_in_huge_damaged_env(tmp_path):
    old = "a" * 64
    backup = tmp_path / ".env.abime-20260928-163313"
    # Fichier abîmé : zéros binaires, énorme, la bonne clé au milieu, un faux candidat avant.
    with open(backup, "wb") as f:
        f.write(b"\x00" * (3 << 20) + b"SECRET_KEY=faussecle12345\n")
        f.write(b"\x00" * (2 << 20) + f"ADMIN_USERNAME=admin\nSECRET_KEY={old}\n".encode() + b"\x00" * 1000)
    tokens = [("MariaDB", _token("SECRET_KEY", old)), ("HFSQL", _token("SECRET_KEY", old))]
    best, ok = rc.find_best([str(backup)], tokens, ("SECRET_KEY", "nouvelle-cle"))
    assert best == ("SECRET_KEY", old) and ok == ["MariaDB", "HFSQL"]

    env = tmp_path / ".env"
    env.write_text("ADMIN_USERNAME=admin\nSECRET_KEY=nouvelle-cle\nENCRYPTION_KEY=x\nAPP_PORT=8000\n", encoding="utf-8")
    rc.set_env(str(env), *best)
    assert env.read_text(encoding="utf-8").splitlines() == ["ADMIN_USERNAME=admin", "APP_PORT=8000", f"SECRET_KEY={old}"]


def test_current_key_kept_when_it_decrypts_more(tmp_path):
    backup = tmp_path / ".env.abime-1"
    backup.write_text("SECRET_KEY=ancienne\n", encoding="utf-8")
    tokens = [("A", _token("SECRET_KEY", "actuelle")), ("B", _token("SECRET_KEY", "actuelle"))]
    best, ok = rc.find_best([str(backup)], tokens, ("SECRET_KEY", "actuelle"))
    assert best is None and ok == ["A", "B"]
