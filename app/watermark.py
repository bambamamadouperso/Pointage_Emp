"""Encodage/décodage de la dernière valeur synchronisée d'une colonne incrémentale."""
import json
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Optional


def encode(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        payload = {"t": "datetime", "v": value.isoformat()}
    elif isinstance(value, date):
        payload = {"t": "date", "v": value.isoformat()}
    elif isinstance(value, time):
        payload = {"t": "time", "v": value.isoformat()}
    elif isinstance(value, bool):
        payload = {"t": "int", "v": int(value)}
    elif isinstance(value, int):
        payload = {"t": "int", "v": value}
    elif isinstance(value, Decimal):
        payload = {"t": "decimal", "v": str(value)}
    elif isinstance(value, float):
        payload = {"t": "float", "v": value}
    else:
        payload = {"t": "str", "v": str(value)}
    return json.dumps(payload)


def decode(raw: Optional[str]) -> Any:
    if not raw:
        return None
    try:
        payload = json.loads(raw)
        kind, value = payload["t"], payload["v"]
    except (ValueError, KeyError, TypeError):
        return raw
    if kind == "datetime":
        return datetime.fromisoformat(value)
    if kind == "date":
        return date.fromisoformat(value)
    if kind == "time":
        return time.fromisoformat(value)
    if kind == "int":
        return int(value)
    if kind == "decimal":
        return Decimal(value)
    if kind == "float":
        return float(value)
    return value
