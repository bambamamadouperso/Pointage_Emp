"""Source Smartsheet : lecture de feuilles via l'API REST (jeton d'accès), au même format que Google Sheets.

La connexion stocke :
- host : serveur de l'API (api.smartsheet.com, ou api.smartsheet.eu pour un compte hébergé en Europe) ;
- database : feuille(s) à lire, séparées par des virgules : identifiant numérique (Fichier → Propriétés → ID de la
  feuille) ou nom exact de la feuille ;
- password_enc : jeton d'accès API (chiffré), créé dans Smartsheet → Compte → Apps & Integrations → API Access.

Chaque feuille devient une « table » (comme un onglet Google Sheets) : colonnes normalisées, valeurs typées
(dates, cases à cocher, nombres), plus une colonne row_id (identifiant stable de la ligne, utilisable comme clé).
"""
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from typing import Any, Optional

from .crypto import decrypt
from .gsheet import SheetData, normalize_identifier

HOSTS = {
    "api.smartsheet.com": "Smartsheet (app.smartsheet.com)",
    "api.smartsheet.eu": "Smartsheet Europe (app.smartsheet.eu)",
}
DEFAULT_HOST = "api.smartsheet.com"
TIMEOUT = 60
PAGE_SIZE = 2500
MAX_PAGES = 200
# Colonnes dont la valeur utile est le texte affiché (contacts, listes à choix multiples…).
_DISPLAY_TYPES = {"CONTACT_LIST", "MULTI_CONTACT_LIST", "MULTI_PICKLIST", "PREDECESSOR", "DURATION"}


class SmartsheetError(Exception):
    pass


def _token(conn) -> str:
    token = decrypt(conn.password_enc) if conn.password_enc else ""
    if not token:
        raise SmartsheetError("Jeton d'accès API Smartsheet manquant.")
    return token


def _api(conn, path: str, params: Optional[dict] = None) -> dict:
    host = conn.host if conn.host in HOSTS else DEFAULT_HOST
    url = f"https://{host}/2.0{path}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {_token(conn)}",
                                                   "Accept": "application/json", "User-Agent": "pointage-sync"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        try:
            message = json.loads(exc.read()).get("message", "")
        except ValueError:
            message = ""
        if exc.code == 401:
            raise SmartsheetError("Jeton d'accès refusé : vérifiez-le (ou créez-en un nouveau) et le serveur "
                                  "(compte européen : api.smartsheet.eu).") from exc
        if exc.code == 404:
            raise SmartsheetError("Feuille introuvable : vérifiez son identifiant et qu'elle est partagée avec le "
                                  "compte du jeton.") from exc
        if exc.code == 403:
            raise SmartsheetError(f"Accès refusé par Smartsheet : {message or 'droits insuffisants'}.") from exc
        if exc.code == 429:
            raise SmartsheetError("Trop de requêtes vers Smartsheet : réessayez dans une minute.") from exc
        raise SmartsheetError(f"Erreur Smartsheet ({exc.code}) : {message or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise SmartsheetError(f"Serveur Smartsheet injoignable ({host}) : {exc.reason}") from exc


def sheet_refs(conn) -> list[str]:
    return [r.strip() for r in re.split(r"[,;\n]+", conn.database or "") if r.strip()]


def list_sheets(conn) -> list[dict]:
    """Feuilles accessibles avec le jeton : [{id, name}]."""
    out, page = [], 1
    while page <= MAX_PAGES:
        data = _api(conn, "/sheets", {"pageSize": 1000, "page": page})
        out += [{"id": str(s["id"]), "name": s.get("name", "")} for s in data.get("data", [])]
        if page >= int(data.get("totalPages") or 1):
            break
        page += 1
    return out


def _resolve(conn, refs: list[str]) -> list[str]:
    """Identifiants des feuilles : les références non numériques sont cherchées par nom."""
    if all(r.isdigit() for r in refs):
        return refs
    by_name = {s["name"].strip().lower(): s["id"] for s in list_sheets(conn)}
    ids = []
    for ref in refs:
        if ref.isdigit():
            ids.append(ref)
        elif ref.lower() in by_name:
            ids.append(by_name[ref.lower()])
        else:
            raise SmartsheetError(f"Aucune feuille nommée « {ref} » n'est accessible avec ce jeton.")
    return ids


def cell_value(cell: dict, column_type: str) -> Any:
    """Valeur typée d'une cellule (date, date-heure, booléen, nombre ou texte)."""
    value = cell.get("value")
    if column_type in _DISPLAY_TYPES or (value is None and cell.get("displayValue")):
        value = cell.get("displayValue", value)
    if value is None:
        return None
    if column_type == "CHECKBOX":
        return bool(value)
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        if column_type == "DATE" and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return date.fromisoformat(value)
        if column_type in ("DATETIME", "ABSTRACT_DATETIME") or re.fullmatch(r"\d{4}-\d{2}-\d{2}T[\d:.]+Z?", value):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
            except ValueError:
                return value
    return value


def parse_sheet(data: dict) -> SheetData:
    """Réponse de GET /sheets/{id} → SheetData (row_id + une colonne par colonne de la feuille)."""
    columns_raw = list(data.get("columns", []))
    seen, columns, headers, ids, types = {"row_id"}, ["row_id"], ["Row ID"], [], []
    for i, col in enumerate(columns_raw):
        name = base = normalize_identifier(col.get("title"), i)
        n = 2
        while name in seen:
            name = f"{base}_{n}"
            n += 1
        seen.add(name)
        columns.append(name)
        headers.append(col.get("title") or "")
        ids.append(col.get("id"))
        types.append(col.get("type", "TEXT_NUMBER"))
    rows = []
    for row in data.get("rows", []):
        cells = {c.get("columnId"): c for c in row.get("cells", [])}
        values = [cell_value(cells.get(cid, {}), typ) for cid, typ in zip(ids, types)]
        if any(v is not None for v in values):
            rows.append([row.get("id")] + values)
    return SheetData(data.get("name") or str(data.get("id", "")), columns, headers, rows)


def fetch_sheet(conn, sheet_id: str) -> SheetData:
    merged: Optional[dict] = None
    page = 1
    while page <= MAX_PAGES:
        data = _api(conn, f"/sheets/{sheet_id}", {"pageSize": PAGE_SIZE, "page": page})
        if merged is None:
            merged = data
        else:
            merged["rows"] = merged.get("rows", []) + data.get("rows", [])
        total = int(data.get("totalRowCount") or 0)
        if not data.get("rows") or len(merged.get("rows", [])) >= total:
            break
        page += 1
    return parse_sheet(merged or {})


def load_sheets(conn) -> dict[str, SheetData]:
    refs = sheet_refs(conn)
    if not refs:
        raise SmartsheetError("Indiquez au moins une feuille (identifiant ou nom).")
    sheets: dict[str, SheetData] = {}
    for sheet_id in _resolve(conn, refs):
        sheet = fetch_sheet(conn, sheet_id)
        name, n = sheet.title, 2
        while name in sheets:
            name = f"{sheet.title} ({n})"
            n += 1
        sheet.title = name
        sheets[name] = sheet
    return sheets


def describe(conn) -> str:
    """Test de connexion : feuilles lues, ou feuilles accessibles si aucune n'est encore choisie."""
    if not sheet_refs(conn):
        found = list_sheets(conn)
        listing = ", ".join(f"{s['name']} ({s['id']})" for s in found[:10]) + ("…" if len(found) > 10 else "")
        return f"jeton valide : {len(found)} feuille(s) accessible(s) — {listing or 'aucune'}"
    sheets = load_sheets(conn)
    return "feuille(s) accessible(s) : " + ", ".join(f"{name} ({len(s.rows)} ligne(s))" for name, s in sheets.items())
