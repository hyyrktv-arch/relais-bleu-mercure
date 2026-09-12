"""Stockage des tours : Supabase si configuré, sinon SQLite local (éphémère sur Streamlit Cloud)."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pandas as pd
import requests

LAP_COLS = ["lap_id", "driver", "car", "track", "session_type", "lap_time", "fuel_used",
            "fuel_level", "clean", "start_time", "raw"]
EXTRA_COLS = ["air_temp", "track_temp", "track_state", "position"]  # écrits par l'agent


def _prep(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for c in LAP_COLS:
        if c not in out.columns:
            out[c] = None
    out = out[LAP_COLS]
    out["clean"] = out["clean"].fillna(True).astype(bool)
    out["start_time"] = pd.to_datetime(out["start_time"], errors="coerce", utc=True)
    return out


def _post(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    df["start_time"] = pd.to_datetime(df["start_time"], errors="coerce", utc=True)
    df["clean"] = df["clean"].fillna(1).astype(int).astype(bool) if df["clean"].dtype != bool else df["clean"]
    for c in ("lap_time", "fuel_used", "fuel_level"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


class SqliteStore:
    def __init__(self, path: str = "relais.db"):
        self.path = path

    def save_laps(self, df: pd.DataFrame) -> int:
        if df.empty:
            return 0
        df = _prep(df)
        with sqlite3.connect(self.path) as con:
            try:
                existing = set(pd.read_sql("SELECT lap_id FROM laps", con)["lap_id"])
            except Exception:  # noqa: BLE001
                existing = set()
            new = df[~df["lap_id"].isin(existing)].copy()
            new["start_time"] = new["start_time"].astype(str)
            new["imported_at"] = datetime.now(timezone.utc).isoformat()
            new.to_sql("laps", con, if_exists="append", index=False)
            return len(new)

    def load_laps(self) -> pd.DataFrame:
        try:
            with sqlite3.connect(self.path) as con:
                return _post(pd.read_sql("SELECT * FROM laps", con))
        except Exception:  # noqa: BLE001
            return pd.DataFrame()

    def clear_laps(self) -> None:
        with sqlite3.connect(self.path) as con:
            con.execute("DROP TABLE IF EXISTS laps")

    def load_live(self) -> pd.DataFrame:
        return pd.DataFrame()

    def load_recent_laps(self, hours: int = 12) -> pd.DataFrame:
        df = self.load_laps()
        if df.empty:
            return df
        return df[df["start_time"] >= pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=hours)]

    label = "SQLite local (éphémère)"


class SupabaseStore:
    """Accès direct à l'API REST PostgREST de Supabase (pas de SDK, juste requests)."""

    def __init__(self, url: str, key: str, team_code: str):
        url = url.strip().rstrip("/")
        for suffix in ("/rest/v1", "/rest"):
            if url.endswith(suffix):
                url = url[: -len(suffix)]
        self.base = url + "/rest/v1/"
        self.team = team_code
        # Nouvelles clés Supabase (sb_secret_… / sb_publishable_…) : en-tête apikey seul.
        # Anciennes clés JWT (eyJ…) : apikey + Authorization Bearer.
        self.h = {"apikey": key, "Content-Type": "application/json"}
        if key.startswith("eyJ"):
            self.h["Authorization"] = f"Bearer {key}"
        self.label = f"Supabase · équipe {team_code}"

    def _rows(self, df: pd.DataFrame) -> list[dict]:
        rows = []
        for _, r in df.iterrows():
            rows.append({
                "lap_id": str(r["lap_id"]), "team_code": self.team, "driver": r["driver"] or "Inconnu",
                "car": r["car"], "track": r["track"], "session_type": r["session_type"],
                "lap_time": None if pd.isna(r["lap_time"]) else float(r["lap_time"]),
                "fuel_used": None if pd.isna(r["fuel_used"]) else float(r["fuel_used"]),
                "fuel_level": None if pd.isna(r["fuel_level"]) else float(r["fuel_level"]),
                "clean": bool(r["clean"]),
                "start_time": None if pd.isna(r["start_time"]) else r["start_time"].isoformat(),
                "raw": json.loads(r["raw"]) if isinstance(r["raw"], str) and r["raw"].startswith("{") else None,
            })
        return rows

    def save_laps(self, df: pd.DataFrame) -> int:
        if df.empty:
            return 0
        df = _prep(df)
        before = self._count()
        for i in range(0, len(df), 200):
            r = requests.post(self.base + "laps", headers={**self.h, "Prefer": "resolution=ignore-duplicates,return=minimal"},
                              json=self._rows(df.iloc[i:i + 200]), timeout=30)
            if not r.ok:
                raise RuntimeError(f"Supabase {r.status_code} : {r.text[:300]}")
        return max(0, self._count() - before)

    def _count(self) -> int:
        r = requests.get(self.base + f"laps?team_code=eq.{self.team}&select=lap_id",
                         headers={**self.h, "Prefer": "count=exact", "Range": "0-0"}, timeout=30)
        cr = r.headers.get("Content-Range", "*/0")
        try:
            return int(cr.split("/")[-1])
        except ValueError:
            return 0

    def load_laps(self) -> pd.DataFrame:
        rows, offset, page = [], 0, 1000
        while True:
            r = requests.get(self.base + f"laps?team_code=eq.{self.team}&select=*&order=start_time.desc",
                             headers={**self.h, "Range": f"{offset}-{offset + page - 1}"}, timeout=60)
            if not r.ok:
                raise RuntimeError(f"Supabase {r.status_code} : {r.text[:300]}")
            chunk = r.json()
            rows.extend(chunk)
            if len(chunk) < page:
                break
            offset += page
        df = pd.DataFrame(rows)
        if df.empty:
            return df
        df["raw"] = df["raw"].apply(lambda v: json.dumps(v) if isinstance(v, dict) else v)
        return _post(df)

    def clear_laps(self) -> None:
        r = requests.delete(self.base + f"laps?team_code=eq.{self.team}", headers=self.h, timeout=60)
        if not r.ok:
            raise RuntimeError(f"Supabase {r.status_code} : {r.text[:300]}")

    def load_live(self) -> pd.DataFrame:
        r = requests.get(self.base + f"live?team_code=eq.{self.team}&select=*&order=updated_at.desc",
                         headers=self.h, timeout=15)
        df = pd.DataFrame(r.json()) if r.ok else pd.DataFrame()
        if not df.empty:
            df["updated_at"] = pd.to_datetime(df["updated_at"], errors="coerce", utc=True)
        return df

    def load_recent_laps(self, hours: int = 12) -> pd.DataFrame:
        since = (pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=hours)).isoformat()
        r = requests.get(self.base + f"laps?team_code=eq.{self.team}&start_time=gte.{since}&select=*&order=start_time.desc",
                         headers={**self.h, "Range": "0-1999"}, timeout=30)
        df = pd.DataFrame(r.json()) if r.ok else pd.DataFrame()
        if df.empty:
            return df
        df["raw"] = df["raw"].apply(lambda v: json.dumps(v) if isinstance(v, dict) else v)
        return _post(df)


def get_store(secrets) -> SqliteStore | SupabaseStore:
    url, key = secrets.get("SUPABASE_URL"), secrets.get("SUPABASE_SERVICE_KEY")
    if url and key:
        return SupabaseStore(url, key, secrets.get("TEAM_CODE", "bleu-mercure"))
    return SqliteStore()
