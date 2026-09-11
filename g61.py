"""Client Garage 61 minimal + cache SQLite.

API : https://garage61.net/api/v1/  (doc : https://garage61.net/developer)
Auth : Personal Access Token (Bearer) — à créer dans ton compte Garage 61.
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone

import pandas as pd
import requests

BASE_URL = "https://garage61.net/api/v1/"
DB_PATH = "relais.db"

SESSION_TYPES = {1: "Practice", 2: "Qualifying", 3: "Race"}
LAP_TYPES = {1: "Normal", 2: "Joker", 3: "Out lap", 4: "In lap"}


class G61Client:
    def __init__(self, token: str, log=None):
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {token}"
        self.log = log or (lambda msg: None)

    def _get(self, endpoint: str, **params) -> dict | list:
        clean = {}
        for k, v in params.items():
            if v is None:
                continue
            clean[k] = ",".join(map(str, v)) if isinstance(v, list) else v
        for attempt in range(2):
            r = self.s.get(BASE_URL + endpoint, params=clean, timeout=30)
            if r.status_code == 429 and attempt == 0:
                wait = 30
                try:
                    wait = int(r.json().get("details", {}).get("retryAfterSeconds", wait))
                except Exception:  # noqa: BLE001
                    pass
                if wait > 60:
                    raise RuntimeError(f"Garage 61 demande d'attendre {wait} s avant de réessayer.")
                self.log(f"Limite de débit : attente {wait} s…")
                time.sleep(wait + 1)
                continue
            if not r.ok:
                raise RuntimeError(f"HTTP {r.status_code} sur {endpoint} — {r.text[:500]}")
            return r.json()
        raise RuntimeError("Garage 61 : limite de débit atteinte, réessaie dans quelques minutes.")

    # --- référentiels -----------------------------------------------------
    def me(self) -> dict:
        return self._get("me")

    def teams(self) -> list[dict]:
        return self._get("teams").get("items", [])

    def cars(self) -> pd.DataFrame:
        return _catalog(self._get("cars").get("items", []))

    def tracks(self) -> pd.DataFrame:
        return _catalog(self._get("tracks").get("items", []))

    # --- tours ------------------------------------------------------------
    def laps(
        self,
        team_slug: str | None = None,
        cars: list[int] | None = None,
        tracks: list[int] | None = None,
        age_days: int | None = 90,
        session_types: list[int] | None = None,
        include_unclean: bool | None = None,
        limit: int = 100,
    ) -> pd.DataFrame:
        """Tous les tours (group=none) de l'équipe, paginés."""
        rows: list[dict] = []
        seen: set = set()
        offset = 0
        for _page in range(15):  # 15 pages max = 1500 tours
            data = self._get(
                "laps",
                teams=team_slug,
                cars=cars,
                tracks=tracks,
                age=age_days,
                sessionTypes=session_types,
                lapTypes=[1],           # tours complets uniquement
                unclean=None if include_unclean is None else str(include_unclean).lower(),
                group="none",
                limit=limit,
                offset=offset,
            )
            items = data.get("items", [])
            self.log(f"Page {_page + 1} : {len(items)} tours reçus (total {len(rows) + len(items)})")
            fresh = [it for it in items if str(it.get("id")) not in seen]
            seen.update(str(it.get("id")) for it in items)
            rows.extend(fresh)
            if len(items) < limit or not fresh:
                break
            offset += limit
            time.sleep(1.5)  # ménage la limite de débit
        return normalize_laps(rows)


def _catalog(items: list[dict]) -> pd.DataFrame:
    """Liste id/nom triée, pour les menus déroulants (voitures, circuits)."""
    rows = []
    for it in items:
        name = _pick(it, "name", "displayName", "title", default=str(it.get("id")))
        variant = _pick(it, "variant", "configName", "layout")
        if variant and variant not in name:
            name = f"{name} — {variant}"
        rows.append({"id": it.get("id"), "name": name})
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    dup = df["name"].duplicated(keep=False)
    df.loc[dup, "name"] = df.loc[dup, "name"] + " (#" + df.loc[dup, "id"].astype(str) + ")"
    return df.sort_values("name").reset_index(drop=True)


def _pick(d: dict, *keys, default=None):
    """Retourne la première clé présente (les noms exacts peuvent varier selon la doc)."""
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


LAST_RAW: list[dict] = []


def _name(obj, *extra_keys) -> str | None:
    """Extrait un nom lisible d'un objet API (dict) ou d'une chaîne."""
    if obj is None:
        return None
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        n = _pick(obj, "name", "displayName", "fullName", "nickname", *extra_keys)
        if n:
            return str(n)
        fn, ln = obj.get("firstName"), obj.get("lastName")
        if fn or ln:
            return f"{fn or ''} {ln or ''}".strip()
        return str(_pick(obj, "id", "slug", default=""))
    return str(obj)


def normalize_laps(items: list[dict]) -> pd.DataFrame:
    """Aplatis la réponse API dans un format stable pour le moteur de relais.

    Colonnes : lap_id, driver, car, track, session_type, lap_time, fuel_used,
               fuel_level, clean, start_time
    """
    global LAST_RAW
    LAST_RAW = items[:1]
    out = []
    for it in items:
        drv = _pick(it, "driver", "user", "account", "driverName", default=None)
        car = _pick(it, "car", "carName", default=None)
        trk = _pick(it, "track", "trackName", default=None)
        out.append(
            {
                "lap_id": str(_pick(it, "id", "lapId", default=len(out))),
                "driver": _name(drv) or "Inconnu",
                "car": _name(car) or "?",
                "track": _name(trk) or "?",
                "session_type": SESSION_TYPES.get(_pick(it, "sessionType", "session_type"), "?"),
                "lap_time": _pick(it, "lapTime", "lap_time", "time"),
                "fuel_used": _pick(it, "fuelUsed", "fuel_used", "fuelConsumed"),
                "fuel_level": _pick(it, "fuel", "fuelLevel", "fuel_level"),
                "clean": bool(_pick(it, "clean", default=True)),
                "start_time": _pick(it, "startTime", "start_time", "date", "createdAt"),
                "raw": json.dumps(it, ensure_ascii=False)[:4000],
            }
        )
    df = pd.DataFrame(out)
    if not df.empty:
        df["lap_time"] = pd.to_numeric(df["lap_time"], errors="coerce")
        df["fuel_used"] = pd.to_numeric(df["fuel_used"], errors="coerce")
        df["start_time"] = pd.to_datetime(df["start_time"], errors="coerce", utc=True)
    return df


# --- cache local ----------------------------------------------------------
def save_laps(df: pd.DataFrame, path: str = DB_PATH) -> int:
    if df.empty:
        return 0
    with sqlite3.connect(path) as con:
        existing = set()
        try:
            existing = set(pd.read_sql("SELECT lap_id FROM laps", con)["lap_id"])
        except Exception:
            pass
        new = df[~df["lap_id"].isin(existing)].copy()
        new["start_time"] = new["start_time"].astype(str)
        new["imported_at"] = datetime.now(timezone.utc).isoformat()
        new.to_sql("laps", con, if_exists="append", index=False)
        return len(new)


def clear_laps(path: str = DB_PATH) -> None:
    with sqlite3.connect(path) as con:
        con.execute("DROP TABLE IF EXISTS laps")


def load_laps(path: str = DB_PATH) -> pd.DataFrame:
    try:
        with sqlite3.connect(path) as con:
            df = pd.read_sql("SELECT * FROM laps", con)
        df["start_time"] = pd.to_datetime(df["start_time"], errors="coerce", utc=True)
        df["clean"] = df["clean"].fillna(1).astype(int).astype(bool)
        return df
    except Exception:
        return pd.DataFrame()


# --- données de démo (pour tester sans token) ----------------------------
def demo_laps(seed: int = 61) -> pd.DataFrame:
    import numpy as np

    rng = np.random.default_rng(seed)
    drivers = {"Arlan": (128.4, 2.85), "David": (129.1, 2.78), "Max": (127.9, 2.95),
               "Léa": (129.8, 2.70), "Tom": (128.9, 2.88)}
    rows = []
    i = 0
    for d, (base, fuel) in drivers.items():
        for _ in range(60):
            i += 1
            rows.append(
                {
                    "lap_id": f"demo-{i}",
                    "driver": d,
                    "car": "Porsche 992 GT3 R",
                    "track": "Spa-Francorchamps",
                    "session_type": rng.choice(["Practice", "Race"], p=[0.4, 0.6]),
                    "lap_time": base + abs(rng.normal(0, 0.6)) + rng.choice([0, 0, 0, 1.5]),
                    "fuel_used": fuel + rng.normal(0, 0.06),
                    "fuel_level": rng.uniform(20, 100),
                    "clean": rng.random() > 0.08,
                    "start_time": pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=int(rng.integers(0, 30))),
                }
            )
    return pd.DataFrame(rows)
