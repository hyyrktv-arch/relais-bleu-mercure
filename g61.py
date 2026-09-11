"""Client Garage 61 minimal + cache SQLite.

API : https://garage61.net/api/v1/  (doc : https://garage61.net/developer)
Auth : Personal Access Token (Bearer) — à créer dans ton compte Garage 61.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pandas as pd
import requests

BASE_URL = "https://garage61.net/api/v1/"
DB_PATH = "relais.db"

SESSION_TYPES = {1: "Practice", 2: "Qualifying", 3: "Race"}
LAP_TYPES = {1: "Normal", 2: "Joker", 3: "Out lap", 4: "In lap"}


class G61Client:
    def __init__(self, token: str):
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {token}"

    def _get(self, endpoint: str, **params) -> dict | list:
        clean = {}
        for k, v in params.items():
            if v is None:
                continue
            clean[k] = ",".join(map(str, v)) if isinstance(v, list) else v
        r = self.s.get(BASE_URL + endpoint, params=clean, timeout=30)
        r.raise_for_status()
        return r.json()

    # --- référentiels -----------------------------------------------------
    def me(self) -> dict:
        return self._get("me")

    def teams(self) -> list[dict]:
        return self._get("teams").get("items", [])

    def cars(self) -> pd.DataFrame:
        return pd.DataFrame(self._get("cars").get("items", []))

    def tracks(self) -> pd.DataFrame:
        return pd.DataFrame(self._get("tracks").get("items", []))

    # --- tours ------------------------------------------------------------
    def laps(
        self,
        team_slug: str | None = None,
        cars: list[int] | None = None,
        tracks: list[int] | None = None,
        age_days: int | None = 90,
        session_types: list[int] | None = None,
        include_unclean: bool = True,
        limit: int = 500,
    ) -> pd.DataFrame:
        """Tous les tours (group=none) de l'équipe, paginés."""
        rows: list[dict] = []
        offset = 0
        while True:
            data = self._get(
                "laps",
                teams=team_slug,
                cars=cars,
                tracks=tracks,
                age=age_days,
                sessionTypes=session_types,
                lapTypes=[1],           # tours complets uniquement
                unclean=str(include_unclean).lower(),
                group="none",
                limit=limit,
                offset=offset,
            )
            items = data.get("items", [])
            rows.extend(items)
            if len(items) < limit:
                break
            offset += limit
        return normalize_laps(rows)


def _pick(d: dict, *keys, default=None):
    """Retourne la première clé présente (les noms exacts peuvent varier selon la doc)."""
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def normalize_laps(items: list[dict]) -> pd.DataFrame:
    """Aplatis la réponse API dans un format stable pour le moteur de relais.

    Colonnes : lap_id, driver, car, track, session_type, lap_time, fuel_used,
               fuel_level, clean, start_time
    """
    out = []
    for it in items:
        drv = _pick(it, "driver", default={})
        car = _pick(it, "car", default={})
        trk = _pick(it, "track", default={})
        out.append(
            {
                "lap_id": _pick(it, "id"),
                "driver": drv.get("name") if isinstance(drv, dict) else str(drv),
                "car": car.get("name") if isinstance(car, dict) else str(car),
                "track": trk.get("name") if isinstance(trk, dict) else str(trk),
                "session_type": SESSION_TYPES.get(_pick(it, "sessionType"), "?"),
                "lap_time": _pick(it, "lapTime"),
                "fuel_used": _pick(it, "fuelUsed"),
                "fuel_level": _pick(it, "fuel", "fuelLevel"),
                "clean": bool(_pick(it, "clean", default=True)),
                "start_time": _pick(it, "startTime", "time"),
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


def load_laps(path: str = DB_PATH) -> pd.DataFrame:
    try:
        with sqlite3.connect(path) as con:
            df = pd.read_sql("SELECT * FROM laps", con)
        df["start_time"] = pd.to_datetime(df["start_time"], errors="coerce", utc=True)
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
