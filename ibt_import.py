"""Import de fichiers de télémétrie iRacing (.ibt) sans passer par Garage 61.

iRacing écrit un .ibt par session dans Documents/iRacing/telemetry (si l'enregistrement
est activé : Options > Misc > Telemetry, ou touche Alt+L en session).
"""
from __future__ import annotations

import json
import struct
from datetime import datetime, timezone

import irsdk
import pandas as pd
import yaml

SESSION_TYPE_MAP = {
    "Practice": "Practice", "Open Practice": "Practice", "Warmup": "Practice",
    "Qualify": "Qualifying", "Lone Qualify": "Qualifying", "Open Qualify": "Qualifying",
    "Race": "Race",
}


def _session_yaml(ibt: irsdk.IBT) -> dict:
    h = ibt._header
    raw = ibt._shared_mem[h.session_info_offset: h.session_info_offset + h.session_info_len]
    text = raw.rstrip(b"\x00").decode("latin-1", errors="ignore")
    try:
        return yaml.safe_load(text) or {}
    except yaml.YAMLError:
        # iRacing produit parfois des valeurs non échappées ; on garde le début lisible
        return yaml.safe_load(text.split("\nCameraInfo:")[0]) or {}


def read_ibt(path: str, source_name: str | None = None) -> pd.DataFrame:
    """Retourne les tours d'un .ibt au format de g61.normalize_laps (+ colonne raw)."""
    ibt = irsdk.IBT()
    ibt.open(path)
    try:
        info = _session_yaml(ibt)
        weekend = info.get("WeekendInfo", {}) or {}
        drv_info = info.get("DriverInfo", {}) or {}
        my_idx = drv_info.get("DriverCarIdx", 0)
        me = next((d for d in drv_info.get("Drivers", []) if d.get("CarIdx") == my_idx), {}) or {}
        driver = me.get("UserName") or me.get("TeamName") or "Inconnu"
        car = me.get("CarScreenName") or me.get("CarPath") or "?"
        track = weekend.get("TrackDisplayName") or "?"
        cfg = weekend.get("TrackConfigName")
        if cfg and str(cfg).strip() and str(cfg).lower() != "none":
            track = f"{track} - {cfg}"
        sessions = {s.get("SessionNum", i): s.get("SessionType", "?")
                    for i, s in enumerate(info.get("SessionInfo", {}).get("Sessions", []) or [])}

        start = ibt._disk_header.session_start_date
        start_dt = datetime.fromtimestamp(start, tz=timezone.utc) if start else None

        need = ["LapCompleted", "LapLastLapTime", "FuelLevel", "SessionNum", "SessionTime", "OnPitRoad", "IsOnTrack"]
        names = set(ibt.var_headers_names or [])
        missing = [n for n in need if n not in names]
        if missing:
            raise ValueError(f"Canaux absents du fichier : {', '.join(missing)}")
        cols = {n: ibt.get_all(n) for n in need}
    finally:
        ibt.close()

    df = pd.DataFrame(cols)
    laps = []
    src = source_name or path.rsplit("/", 1)[-1]
    for sess_num, g in df.groupby("SessionNum", sort=True):
        g = g.reset_index(drop=True)
        session_type = SESSION_TYPE_MAP.get(str(sessions.get(sess_num, "")), "?")
        # début de chaque tour = premier tick où LapCompleted prend une nouvelle valeur
        change = g["LapCompleted"].diff().fillna(0) != 0
        idx = list(g.index[change])
        for k, end_i in enumerate(idx):
            start_i = idx[k - 1] if k > 0 else 0
            seg = g.iloc[start_i:end_i]
            if seg.empty:
                continue
            lap_time = float(g.loc[end_i, "LapLastLapTime"])
            if lap_time <= 0:
                continue
            fuel_start = float(seg["FuelLevel"].iloc[0])
            fuel_end = float(g.loc[end_i, "FuelLevel"])
            fuel_used = fuel_start - fuel_end
            pit = bool(seg["OnPitRoad"].any()) or bool(g.loc[end_i, "OnPitRoad"])
            off_track = not bool(seg["IsOnTrack"].all())
            refuel = fuel_used < 0
            clean = not (pit or refuel or off_track)
            lap_no = int(g.loc[end_i, "LapCompleted"])
            t_end = float(g.loc[end_i, "SessionTime"])
            laps.append({
                "lap_id": f"ibt:{src}:{sess_num}:{lap_no}",
                "driver": driver, "car": car, "track": track, "session_type": session_type,
                "lap_time": round(lap_time, 3),
                "fuel_used": round(fuel_used, 3) if clean else None,
                "fuel_level": round(fuel_end, 2),
                "clean": clean,
                "start_time": start_dt + pd.Timedelta(seconds=t_end) if start_dt else None,
                "raw": json.dumps({"source": "ibt", "file": src, "session": int(sess_num), "lap": lap_no,
                                   "pit": pit, "off_track": off_track, "refuel": refuel}),
            })
    out = pd.DataFrame(laps)
    if not out.empty:
        out["start_time"] = pd.to_datetime(out["start_time"], errors="coerce", utc=True)
    return out


# --- générateur de fichier de test (utilisé uniquement pour valider le parseur) ---------
def write_fake_ibt(path: str, n_laps: int = 5, tick_rate: int = 10) -> None:  # pragma: no cover
    """Écrit un .ibt minimal mais conforme au format iRacing, pour tests."""
    session_yaml = yaml.safe_dump({
        "WeekendInfo": {"TrackDisplayName": "Circuit de Spa-Francorchamps", "TrackConfigName": "Endurance"},
        "SessionInfo": {"Sessions": [{"SessionNum": 0, "SessionType": "Practice"}]},
        "DriverInfo": {"DriverCarIdx": 3, "Drivers": [
            {"CarIdx": 3, "UserName": "Test Pilote", "CarScreenName": "Ligier JS P320"}]},
    }, allow_unicode=True).encode("latin-1", errors="replace") + b"\x00"

    vars_ = [("LapCompleted", 2), ("LapLastLapTime", 4), ("FuelLevel", 4), ("SessionNum", 2),
             ("SessionTime", 5), ("OnPitRoad", 1), ("IsOnTrack", 1)]
    sizes = {1: 1, 2: 4, 4: 4, 5: 8}
    off, headers = 0, []
    for name, typ in vars_:
        headers.append((name, typ, off))
        off += sizes[typ]
    buf_len = off
    header_len, disk_len = 112, 32
    yaml_off = header_len + disk_len
    vh_off = yaml_off + len(session_yaml)
    buf_off = vh_off + 144 * len(vars_)

    ticks = []
    lap, fuel, t = 0, 100.0, 0.0
    lap_len = 130.0
    per_tick = lap_len / tick_rate
    while lap < n_laps:
        for i in range(tick_rate):
            t += per_tick
            fuel -= 3.9 / tick_rate
            completed = lap
            last = lap_len + 0.3 * lap if lap > 0 else 0.0
            ticks.append((completed, last, fuel, 0, t, False, True))
        lap += 1
    ticks.append((lap, lap_len + 0.3 * (lap - 1), fuel, 0, t + 0.1, False, True))

    fmt = "".join({1: "?", 2: "i", 4: "f", 5: "d"}[typ] for _, typ in vars_)
    data = b"".join(struct.pack("<" + fmt, *tk) for tk in ticks)

    hdr = struct.pack("<iiiiiiiiiiiB3x", 2, 0, tick_rate, 1, len(session_yaml), yaml_off,
                      len(vars_), vh_off, 1, buf_len, len(ticks), 0)
    hdr += struct.pack("<iii4x", len(ticks), buf_off, 0)          # VarBuffer 0
    hdr = hdr.ljust(header_len, b"\x00")
    disk = struct.pack("<Qddii", int(datetime(2026, 9, 12, tzinfo=timezone.utc).timestamp()),
                       0.0, t, n_laps, len(ticks)).ljust(disk_len, b"\x00")
    vhs = b"".join(struct.pack("<iii?3x32s64s32s", typ, o, 1, False, name.encode(), b"", b"")
                   for name, typ, o in headers)
    with open(path, "wb") as f:
        f.write(hdr + disk + session_yaml + vhs + data)
