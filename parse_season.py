"""Extrait le calendrier des séries choisies depuis le PDF de saison iRacing.

Usage : python parse_season.py 2026s4.pdf season.json
Requiert : pdftotext (poppler). Les séries et voitures suivies sont dans TRACKED.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys

# Voitures de l'équipe : toute série qui en contient une, avec des courses > MIN_DURATION min, est retenue.
CARS = ["Ligier JS P320", "Ligier JS P325", "Dallara P217"]
MIN_DURATION = 60
SHORT = {"IMSA Endurance Series": "IMSA Endurance", "IMSA Sportscar Endurance Challenge": "IMSA Sportscar Endurance"}

WEEK_RE = re.compile(r"^Week (\d+) \((\d{4}-\d{2}-\d{2})\)\s+(.+?)\s+(\d+°F/\d+°C.*)$")
SIM_TIME_RE = re.compile(r"\((\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}) (\d+)x\)")
TEMP_RE = re.compile(r"(\d+)°F/(\d+)°C")
RAIN_RE = re.compile(r"Rain chance (None|\d+%)")
DUR_RE = re.compile(r"(\d+)\s*$")  # durée en fin de 1re ligne d'info (ex : 'Grid by 160' + 'mins' ligne suivante)
GMT_RE = re.compile(r"Races? (.+GMT)")
FUEL_RE = re.compile(r"^\s*([A-Z0-9]+): fuel: (\d+)%")


def pdf_text(path: str) -> str:
    return subprocess.run(["pdftotext", "-layout", path, "-"], capture_output=True, text=True, check=True).stdout


def split_sections(text: str) -> list[list[str]]:
    sections, cur = [], []
    for line in text.split("\n"):
        if line.startswith("\f"):
            if cur:
                sections.append(cur)
            cur = [line.lstrip("\f")]
        else:
            cur.append(line)
    if cur:
        sections.append(cur)
    return sections


def parse_series(lines: list[str], name: str, meta: dict, following: list[str]) -> dict:
    """lines = section du titre ; following = lignes des pages suivantes (suite éventuelle du tableau)."""
    header = " ".join(l.strip() for l in lines[:14])
    slot_line = next((l.strip() for l in lines[:14] if l.strip().startswith("Races")), "")
    slots = GMT_RE.search(slot_line)
    team = "Team racing" in header
    races = []
    all_lines = lines + following
    i = 0
    while i < len(all_lines):
        m = WEEK_RE.match(all_lines[i])
        if not m:
            i += 1
            continue
        week, date, track, info = m.groups()
        block = [all_lines[i]]
        j = i + 1
        while j < len(all_lines) and not WEEK_RE.match(all_lines[j]) and not all_lines[j].startswith("\f"):
            block.append(all_lines[j])
            j += 1
        blob = " ".join(b.strip() for b in block)
        # nom de circuit sur 2 lignes (ex : Le Mans) : la 2e ligne commence par des espaces puis du texte, avant la ligne (sim time)
        if not SIM_TIME_RE.search(block[0]) and len(block) > 1:
            cont = block[1].strip()
            if cont and not SIM_TIME_RE.search(cont) and not cont.startswith(("class", "cautions", "start", "Drive", "Qual")):
                track = f"{track} {cont.split('  ')[0].strip()}"
        sim = SIM_TIME_RE.search(blob)
        temp = TEMP_RE.search(blob)
        rain = RAIN_RE.search(blob)
        dur = None
        tail = re.search(r"(\d+)\s*(mins)?\s*$", block[0].rstrip())
        if tail and "mins" in blob:
            dur = int(tail.group(1))
        fuel = {m2.group(1): int(m2.group(2)) for m2 in (FUEL_RE.match(b) for b in block) if m2}
        drivers = re.search(r"Min (\d+) driver, Max (\d+) drivers", blob)
        races.append({
            "week": int(week), "date": date, "track": re.sub(r"\s+", " ", track).strip(),
            "duration_min": dur,
            "sim_start": f"{sim.group(1)} {sim.group(2)}" if sim else None,
            "time_accel": int(sim.group(3)) if sim else None,
            "temp_c": int(temp.group(2)) if temp else None,
            "rain": (0 if (rain and rain.group(1) == "None") else (int(rain.group(1).rstrip("%")) if rain else None)),
            "min_drivers": int(drivers.group(1)) if drivers else None,
            "max_drivers": int(drivers.group(2)) if drivers else None,
            "fuel_limits": fuel or None,
            "rolling_start": "Rolling start" in blob,
        })
        i = j
        if len(races) >= 12:
            break
    return {
        "series": name, "short": meta["short"], "car": meta["car"], "team_racing": team,
        "slots_gmt": slots.group(1) if slots else None, "races": races,
    }


def main(pdf: str, out: str) -> None:
    text = pdf_text(pdf)
    sections = split_sections(text)
    result = []
    for idx, sec in enumerate(sections):
        title = next((l.strip() for l in sec[:2] if " - " in l and "Season" in l), sec[0].strip())
        head = " ".join(l.strip() for l in sec[:12])
        hit = [c for c in CARS if c in head]
        if not hit:
            continue
        name = title.split(" - 20")[0].strip()
        following = []
        for nxt in sections[idx + 1: idx + 3]:
            if re.match(r"^Week \d+", nxt[0].strip()):
                following += nxt
            else:
                break
        parsed = parse_series(sec, name, {"car": ", ".join(hit), "short": SHORT.get(name, name)}, following)
        durs = [r["duration_min"] for r in parsed["races"] if r["duration_min"]]
        if durs and max(durs) > MIN_DURATION:
            result.append(parsed)
    season = re.search(r"(\d{4}) Season (\d)", text)
    data = {"season": f"{season.group(1)} S{season.group(2)}" if season else "?", "series": result}
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    for s in result:
        print(f"{s['series']} ({s['car']}) : {len(s['races'])} courses, créneaux {s['slots_gmt']}")
        for r in s["races"]:
            print(f"  W{r['week']} {r['date']} {r['track']} — {r['duration_min']} min, {r['temp_c']}°C, pluie {r['rain']}, sim {r['sim_start']}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "season.json")
