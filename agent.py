"""Agent Relais Bleu Mercure — envoie la télémétrie iRacing du pilote vers l'équipe.

Fonctionnement : lit la mémoire partagée iRacing (pyirsdk), détecte chaque tour bouclé,
l'envoie dans la table `laps` de Supabase ; met à jour la table `live` toutes les 5 s en
session (carburant restant, tour, temps restant) pour le suivi en course.

Lancement : python agent.py            (première fois : demande le code équipe)
            python agent.py --simulate (test sans iRacing)
Le fichier agent_config.json est créé à côté du script.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import requests

# --- Configuration par défaut (côté équipe : URL et clé PUBLISHABLE, qui peut circuler) ----
DEFAULT_SUPABASE_URL = "https://nnflcfueaqxomxtdldjh.supabase.co"
DEFAULT_PUBLISHABLE_KEY = "sb_publishable_0r44NmqRVcVR_u-MaG4oWg_U2zKVZnh"

APP_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) if not getattr(sys, "frozen", False) else Path(sys.executable).parent
CONFIG_PATH = Path(os.environ.get("AGENT_CONFIG", APP_DIR / "agent_config.json"))
QUEUE_PATH = CONFIG_PATH.with_name("agent_queue.jsonl")

SESSION_TYPE_MAP = {
    "Practice": "Practice", "Open Practice": "Practice", "Warmup": "Practice", "Offline Testing": "Practice",
    "Testing": "Practice", "Time Trial": "Practice",
    "Qualify": "Qualifying", "Lone Qualify": "Qualifying", "Open Qualify": "Qualifying",
    "Race": "Race",
}


SKIES = {0: "Dégagé", 1: "Peu nuageux", 2: "Nuageux", 3: "Couvert"}
WETNESS = {0: "Inconnu", 1: "Sèche", 2: "Légèrement humide", 3: "Humide", 4: "Très humide",
           5: "Mouillée", 6: "Très mouillée", 7: "Détrempée"}


def _get(ir, key, default=None):
    try:
        v = ir[key]
        return default if v is None else v
    except Exception:  # noqa: BLE001
        return default


def weather(ir) -> dict:
    """Conditions de piste au moment de la lecture."""
    wet = _get(ir, "TrackWetness")
    skies = _get(ir, "Skies")
    state = WETNESS.get(int(wet), None) if isinstance(wet, (int, float)) else None
    sky = SKIES.get(int(skies), None) if isinstance(skies, (int, float)) else None
    track_state = " · ".join(x for x in (state, sky) if x) or None
    tt = _get(ir, "TrackTempCrew") or _get(ir, "TrackTemp")
    return {
        "air_temp": round(float(_get(ir, "AirTemp", 0)), 1) if _get(ir, "AirTemp") is not None else None,
        "track_temp": round(float(tt), 1) if tt is not None else None,
        "track_state": track_state,
        "position": int(_get(ir, "PlayerCarPosition", 0)) or None,
        "incidents": int(_get(ir, "PlayerCarMyIncidentCount", 0)) if _get(ir, "PlayerCarMyIncidentCount") is not None else None,
    }


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# --- configuration -------------------------------------------------------------------------
def load_config() -> dict:
    cfg = {"team_code": "", "supabase_url": DEFAULT_SUPABASE_URL, "publishable_key": DEFAULT_PUBLISHABLE_KEY,
           "driver_name_override": ""}
    if CONFIG_PATH.exists():
        saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        # l'URL et la clé viennent du programme, sauf si le json les surcharge avec une vraie valeur
        for k, v in saved.items():
            if k in ("supabase_url", "publishable_key") and (not v or "REMPLACER" in v):
                continue
            cfg[k] = v
    cfg["team_code"] = cfg["team_code"].strip().lower().replace(" ", "-")
    if not cfg["team_code"]:
        print("\n=== Agent Relais Bleu Mercure — première configuration ===")
        cfg["team_code"] = input("Code équipe (ex : bleu-mercure) : ").strip().lower().replace(" ", "-")
        CONFIG_PATH.write_text(json.dumps({"team_code": cfg["team_code"], "driver_name_override": ""},
                                          indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Configuration enregistrée dans {CONFIG_PATH}")
        if os.name == "nt":
            rep = input("Lancer l'agent automatiquement au démarrage de Windows ? [O/n] : ").strip().lower()
            if rep in ("", "o", "oui", "y", "yes"):
                install_startup()
        print()
    return cfg


def _startup_dir() -> Path:
    return Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def install_startup() -> None:
    """Crée un raccourci (fenêtre réduite) dans le dossier Démarrage de Windows."""
    try:
        target = Path(sys.executable) if getattr(sys, "frozen", False) else Path(__file__).resolve()
        args = "" if getattr(sys, "frozen", False) else f'"{target}"'
        exe = str(target) if getattr(sys, "frozen", False) else sys.executable
        lnk = _startup_dir() / "RelaisBleuMercureAgent.lnk"
        vbs = f'''Set s = CreateObject("WScript.Shell")
Set l = s.CreateShortcut("{lnk}")
l.TargetPath = "{exe}"
l.Arguments = "{args}"
l.WorkingDirectory = "{target.parent}"
l.WindowStyle = 7
l.Description = "Agent Relais Bleu Mercure"
l.Save'''
        tmp = Path(os.environ.get("TEMP", ".")) / "rbm_shortcut.vbs"
        tmp.write_text(vbs, encoding="utf-8")
        os.system(f'cscript //nologo "{tmp}"')
        tmp.unlink(missing_ok=True)
        print(f"Démarrage automatique activé ({lnk.name}). Pour le retirer : supprimer ce raccourci dans shell:startup.")
    except Exception as e:  # noqa: BLE001
        print(f"Impossible de créer le raccourci de démarrage : {e}")


def uninstall_startup() -> None:
    lnk = _startup_dir() / "RelaisBleuMercureAgent.lnk"
    if lnk.exists():
        lnk.unlink()
        print("Démarrage automatique désactivé.")
    else:
        print("Aucun démarrage automatique configuré.")


# --- envoi Supabase avec file d'attente hors-ligne -------------------------------------------
class Sender:
    def __init__(self, url: str, key: str):
        url = url.strip().rstrip("/")
        for suf in ("/rest/v1", "/rest"):
            if url.endswith(suf):
                url = url[: -len(suf)]
        self.base = url + "/rest/v1/"
        self.h = {"apikey": key, "Content-Type": "application/json", "Prefer": "return=minimal"}
        if key.startswith("eyJ"):
            self.h["Authorization"] = f"Bearer {key}"
        self.queue: deque[dict] = deque()
        self.next_retry = 0.0
        if QUEUE_PATH.exists():
            for line in QUEUE_PATH.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self.queue.append(json.loads(line))
            if self.queue:
                log(f"{len(self.queue)} tours en attente d'envoi (hors-ligne précédent)")

    def _persist_queue(self) -> None:
        QUEUE_PATH.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in self.queue), encoding="utf-8")

    def send_lap(self, row: dict) -> None:
        self.queue.append(row)
        self.flush(force=True)

    def flush(self, force: bool = False) -> None:
        if not self.queue or (not force and time.time() < self.next_retry):
            return
        while self.queue:
            row = self.queue[0]
            try:
                r = requests.post(self.base + "laps", headers={**self.h, "Prefer": "resolution=ignore-duplicates,return=minimal"},
                                  json=row, timeout=10)
                if r.ok or r.status_code == 409:
                    self.queue.popleft()
                    continue
                log(f"Supabase {r.status_code} : {r.text[:200]}")
                self.next_retry = time.time() + 30
                break
            except requests.RequestException as e:
                log(f"Hors-ligne, tour conservé localement ({e.__class__.__name__}), nouvel essai dans 30 s")
                self.next_retry = time.time() + 30
                break
        self._persist_queue()

    def send_live(self, row: dict) -> None:
        try:
            requests.post(self.base + "live?on_conflict=team_code,driver",
                          headers={**self.h, "Prefer": "resolution=merge-duplicates,return=minimal"},
                          json=row, timeout=10)
        except requests.RequestException:
            pass


# --- lecture iRacing -------------------------------------------------------------------------
class LapTracker:
    """Détecte les tours bouclés à partir des variables télémétrie."""

    def __init__(self, cfg: dict, sender: Sender):
        self.cfg, self.sender = cfg, sender
        self.reset()

    def reset(self) -> None:
        self.session_key = None
        self.last_completed = None
        self.fuel_at_lap_start = None
        self.pit_in_lap = False
        self.off_track_in_lap = False
        self.last_live = 0.0

    def context(self, ir) -> dict:
        wk = ir["WeekendInfo"] or {}
        di = ir["DriverInfo"] or {}
        idx = di.get("DriverCarIdx", 0)
        me = next((d for d in di.get("Drivers", []) if d.get("CarIdx") == idx), {}) or {}
        sess_num = ir["SessionNum"] or 0
        sessions = (ir["SessionInfo"] or {}).get("Sessions", []) or []
        stype = next((s.get("SessionType") for s in sessions if s.get("SessionNum") == sess_num), "?")
        track = wk.get("TrackDisplayName") or "?"
        cfgname = wk.get("TrackConfigName")
        if cfgname and str(cfgname).strip().lower() not in ("", "none"):
            track = f"{track} - {cfgname}"
        return {
            "driver": self.cfg.get("driver_name_override") or me.get("UserName") or "Inconnu",
            "car": me.get("CarScreenName") or me.get("CarPath") or "?",
            "track": track,
            "session_type": SESSION_TYPE_MAP.get(str(stype), "?"),
            "raw_type": stype,
            "session_id": f"{wk.get('SessionID', 0)}-{wk.get('SubSessionID', 0)}-{sess_num}",
        }

    def tick(self, ir, now: float) -> None:
        ctx = self.context(ir)
        if ctx["session_id"] != self.session_key:
            self.reset()
            self.session_key = ctx["session_id"]
            self.last_completed = ir["LapCompleted"]
            self.fuel_at_lap_start = ir["FuelLevel"]
            log(f"Session détectée : {ctx['session_type']} (iRacing : {ctx['raw_type']}) — {ctx['car']} @ {ctx['track']} ({ctx['driver']})")

        if ir["OnPitRoad"]:
            self.pit_in_lap = True
        if not ir["IsOnTrack"]:
            self.off_track_in_lap = True

        completed = ir["LapCompleted"]
        if self.last_completed is not None and completed is not None and completed > self.last_completed:
            lap_time = float(ir["LapLastLapTime"] or 0)
            fuel_now = float(ir["FuelLevel"] or 0)
            fuel_used = (self.fuel_at_lap_start - fuel_now) if self.fuel_at_lap_start is not None else None
            refuel = fuel_used is not None and fuel_used < 0
            clean = lap_time > 0 and not (self.pit_in_lap or self.off_track_in_lap or refuel)
            if lap_time > 0:
                row = {
                    "lap_id": f"agent:{self.cfg['team_code']}:{ctx['driver']}:{ctx['session_id']}:{completed}",
                    "team_code": self.cfg["team_code"], "driver": ctx["driver"], "car": ctx["car"],
                    "track": ctx["track"], "session_type": ctx["session_type"],
                    "lap_time": round(lap_time, 3),
                    "fuel_used": round(fuel_used, 3) if (clean and fuel_used is not None) else None,
                    "fuel_level": round(fuel_now, 2), "clean": clean,
                    "start_time": datetime.now(timezone.utc).isoformat(),
                    "raw": {"source": "agent", "lap": int(completed), "pit": self.pit_in_lap,
                            "off_track": self.off_track_in_lap, "refuel": refuel},
                }
                w = weather(ir)
                row.update({k: w[k] for k in ("air_temp", "track_temp", "track_state", "position")})
                self.sender.send_lap(row)
                tag = "" if clean else " (non propre)"
                meteo = f" — piste {w['track_temp']}°C, air {w['air_temp']}°C" if w.get("track_temp") else ""
                log(f"Tour {completed} : {lap_time:.3f} s, {fuel_used if fuel_used is None else round(fuel_used, 2)} L{tag}{meteo}")
            self.last_completed = completed
            self.fuel_at_lap_start = fuel_now
            self.pit_in_lap = bool(ir["OnPitRoad"])
            self.off_track_in_lap = False

        if now - self.last_live >= 5:
            self.last_live = now
            self.sender.send_live({
                "team_code": self.cfg["team_code"], "driver": ctx["driver"], "car": ctx["car"], "track": ctx["track"],
                "session_type": ctx["session_type"], "session_time": ir["SessionTime"],
                "time_remain": ir["SessionTimeRemain"], "lap": ir["Lap"], "fuel_level": ir["FuelLevel"],
                "last_lap_time": ir["LapLastLapTime"], "on_pit_road": bool(ir["OnPitRoad"]),
                "updated_at": datetime.now(timezone.utc).isoformat(),
                **weather(ir),
            })


# --- simulateur (tests sans iRacing) ----------------------------------------------------------
class FakeIR:
    def __init__(self):
        self.t, self.lap, self.fuel = 0.0, 0, 100.0
        self.lap_len = 131.0

    def startup(self):
        return True

    @property
    def is_initialized(self):
        return True

    @property
    def is_connected(self):
        return True

    def freeze_var_buffer_latest(self):
        self.t += 0.5
        self.fuel -= 3.9 / (self.lap_len / 0.5)
        if self.t >= (self.lap + 1) * self.lap_len:
            self.lap += 1

    def __getitem__(self, k):
        return {
            "WeekendInfo": {"TrackDisplayName": "Circuit de Spa-Francorchamps", "TrackConfigName": "Endurance",
                            "SessionID": 1, "SubSessionID": 1},
            "DriverInfo": {"DriverCarIdx": 0, "Drivers": [{"CarIdx": 0, "UserName": "Pilote Test", "CarScreenName": "Ligier JS P320"}]},
            "SessionInfo": {"Sessions": [{"SessionNum": 0, "SessionType": "Practice"}]},
            "SessionNum": 0, "LapCompleted": self.lap, "Lap": self.lap + 1,
            "LapLastLapTime": self.lap_len + 0.2 * self.lap if self.lap > 0 else -1,
            "FuelLevel": self.fuel, "SessionTime": self.t, "SessionTimeRemain": 7200 - self.t,
            "OnPitRoad": False, "IsOnTrack": True,
            "AirTemp": 21.4, "TrackTempCrew": 29.8, "Skies": 1, "TrackWetness": 1,
            "PlayerCarPosition": 3, "PlayerCarMyIncidentCount": 2,
        }.get(k)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--simulate", action="store_true", help="test sans iRacing")
    ap.add_argument("--fast", type=float, default=1.0, help="accélération du simulateur")
    ap.add_argument("--install-startup", action="store_true", help="active le lancement au démarrage de Windows")
    ap.add_argument("--uninstall-startup", action="store_true", help="désactive le lancement au démarrage")
    args = ap.parse_args()
    if args.install_startup:
        install_startup()
        return
    if args.uninstall_startup:
        uninstall_startup()
        return

    cfg = load_config()
    sender = Sender(cfg["supabase_url"], cfg["publishable_key"])
    tracker = LapTracker(cfg, sender)
    log(f"Agent v1.2 démarré — équipe {cfg['team_code']}")
    log(f"Envoi vers {sender.base} (clé {cfg['publishable_key'][:15]}…) — config : {CONFIG_PATH}")
    if "REMPLACER" in sender.base or "REMPLACER" in cfg["publishable_key"]:
        log("ATTENTION : URL ou clé Supabase non renseignées (valeur REMPLACER). Rien ne sera envoyé.")

    if args.simulate:
        ir = FakeIR()
    else:
        import irsdk  # Windows uniquement
        ir = irsdk.IRSDK()

    connected = False
    while True:
        try:
            if not connected:
                if not ir.startup() or not ir.is_initialized or not ir.is_connected:
                    time.sleep(3 if not args.simulate else 0)
                    continue
                connected = True
                log("iRacing connecté")
            if not ir.is_connected:
                connected = False
                tracker.reset()
                log("iRacing déconnecté, en attente…")
                time.sleep(3)
                continue
            ir.freeze_var_buffer_latest()
            tracker.tick(ir, time.time())
            sender.flush()
            time.sleep(0.5 / args.fast)
        except KeyboardInterrupt:
            log("Arrêt demandé")
            break
        except Exception as e:  # noqa: BLE001
            log(f"Erreur : {e}")
            time.sleep(2)


if __name__ == "__main__":
    main()
