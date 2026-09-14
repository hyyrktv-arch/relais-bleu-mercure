"""Calendrier de saison : chargement, créneaux en heure locale, état de préparation."""
from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

PARIS = ZoneInfo("Europe/Paris")
DAY_OFFSET = {"Saturday": 0, "Sunday": 1, "Friday": -1, "Monday": 2}


def load_season(path: str = "season.json") -> dict:
    p = Path(path)
    if not p.exists():
        return {"season": "?", "series": []}
    return json.loads(p.read_text(encoding="utf-8"))


def parse_slots(text: str | None) -> list[tuple[int, int]]:
    """'every other Saturday at 2, 7, 18 GMT and Sunday at 14 GMT' -> [(0,2),(0,7),(0,18),(1,14)]"""
    if not text:
        return []
    out = []
    # découpe par jour : 'Saturday at 2, 7, 18 GMT and Sunday at 0 GMT, 20 GMT'
    for day, rest in re.findall(r"(Saturday|Sunday|Friday|Monday) at ([^A-Za-z]*(?:GMT[^A-Za-z]*)*)", text):
        for h in re.findall(r"\d+", rest):
            out.append((DAY_OFFSET[day], int(h)))
    return sorted(set(out))


def race_slots_local(race_date: str, slots: list[tuple[int, int]]) -> list[datetime]:
    base = datetime.strptime(race_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return [(base + timedelta(days=d, hours=h)).astimezone(PARIS) for d, h in slots]


JOURS = {"Mon": "lun.", "Tue": "mar.", "Wed": "mer.", "Thu": "jeu.", "Fri": "ven.", "Sat": "sam.", "Sun": "dim.",
         "Monday": "lundi", "Tuesday": "mardi", "Wednesday": "mercredi", "Thursday": "jeudi", "Friday": "vendredi",
         "Saturday": "samedi", "Sunday": "dimanche"}


def fr(dt, fmt: str) -> str:
    """strftime avec les jours en français (%A / %a)."""
    out = dt.strftime(fmt)
    for en, f in sorted(JOURS.items(), key=lambda kv: -len(kv[0])):
        out = out.replace(en, f)
    return out


def norm(s: str | None) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = s.split(" - ")[0]
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def match_track(target: str, candidates: list[str]) -> list[str]:
    """Noms de circuits en base qui correspondent au circuit du calendrier (même base de nom)."""
    t = norm(target)
    key = " ".join(t.split()[:2])  # ex : 'circuit de spa', 'road atlanta', 'sebring international'
    return [c for c in candidates if norm(c) == t or norm(c).startswith(t) or t.startswith(norm(c)) or norm(c).startswith(key)]


def readiness(laps: pd.DataFrame, car: str, track: str) -> dict:
    if laps.empty:
        return {"laps": 0, "drivers": [], "conso": None, "pace": None, "tracks": []}
    cars = [c for c in laps["car"].dropna().unique() if norm(c) == norm(car)]
    tracks = match_track(track, list(laps["track"].dropna().unique()))
    sub = laps[laps["car"].isin(cars) & laps["track"].isin(tracks) & laps["clean"].astype(bool)]
    return {
        "laps": int(len(sub)),
        "drivers": sorted(sub["driver"].dropna().unique().tolist()),
        "conso": float(sub["fuel_used"].mean()) if sub["fuel_used"].notna().any() else None,
        "pace": float(sub["lap_time"].mean()) if not sub.empty else None,
        "tracks": tracks, "cars": cars,
    }


def upcoming(season: dict, now: datetime | None = None) -> pd.DataFrame:
    now = now or datetime.now(tz=PARIS)
    rows = []
    for s in season.get("series", []):
        slots = parse_slots(s.get("slots_gmt"))
        for r in s["races"]:
            locs = race_slots_local(r["date"], slots)
            future = [d for d in locs if d > now]
            rows.append({
                "Série": s["short"], "Voiture": s["car"], "Semaine": r["week"], "Date": r["date"],
                "Circuit": r["track"], "Durée (min)": r.get("duration_min"),
                "Créneaux (Paris)": ", ".join(fr(d, "%a %H:%M") for d in locs) or "?",
                "Prochain départ": min(future) if future else None,
                "Météo": f"{r.get('temp_c')}°C, pluie {r.get('rain') or 0}%",
                "Heure sim": r.get("sim_start"), "Team": s.get("team_racing", False),
                "_slots": locs, "_race": r, "_series": s,
            })
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values(["Date", "Série"]).reset_index(drop=True)
    return df
