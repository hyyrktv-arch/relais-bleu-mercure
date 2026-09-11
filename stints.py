"""Moteur de calcul des relais."""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd


def driver_stats(laps: pd.DataFrame, trim_pct: float = 0.10) -> pd.DataFrame:
    """Rythme et conso par pilote, en écartant les tours les plus lents (trafic, erreurs)."""
    if laps.empty:
        return pd.DataFrame()
    laps = laps.dropna(subset=["lap_time"]).copy()
    laps["clean"] = laps["clean"].astype(bool)
    laps = laps[laps["clean"]]
    if laps.empty:
        return pd.DataFrame()
    rows = []
    laps["driver"] = laps["driver"].fillna("Inconnu")
    for drv, g in laps.groupby("driver"):
        g = g.sort_values("lap_time")
        cut = int(len(g) * (1 - trim_pct)) or len(g)
        kept = g.iloc[:cut]
        rows.append(
            {
                "Pilote": drv,
                "Tours": len(g),
                "Meilleur": g["lap_time"].min(),
                "Rythme moyen": kept["lap_time"].mean(),
                "Écart-type": kept["lap_time"].std(),
                "Conso / tour (L)": kept["fuel_used"].mean(),
                "Conso max (L)": g["fuel_used"].quantile(0.9),
                "Tours avec conso": int(g["fuel_used"].notna().sum()),
            }
        )
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values("Rythme moyen").reset_index(drop=True)


@dataclass
class RaceParams:
    duration_min: float = 360         # durée course en minutes
    tank_l: float = 100.0             # capacité réservoir
    pit_loss_s: float = 60.0          # temps perdu par arrêt (entrée+sortie+arrêt)
    refuel_rate_lps: float = 3.0      # litres/seconde au ravitaillement
    fuel_margin_l: float = 2.0        # réserve de sécurité en fin de relais
    max_stint_min: float | None = None  # limite règlementaire (ex : 120 min) ou None
    driver_order: list[str] = field(default_factory=list)


def plan_stints(stats: pd.DataFrame, p: RaceParams) -> pd.DataFrame:
    """Génère le plan de relais en enchaînant les pilotes dans l'ordre donné.

    Hypothèses : plein à chaque arrêt, rythme et conso constants par pilote,
    relais limité par le carburant puis par max_stint_min.
    """
    if stats.empty or not p.driver_order:
        return pd.DataFrame()

    s = stats.set_index("Pilote")
    total_s = p.duration_min * 60
    t = 0.0
    lap_no = 0
    rows = []
    i = 0
    while t < total_s:
        drv = p.driver_order[i % len(p.driver_order)]
        pace = s.loc[drv, "Rythme moyen"]
        cons = s.loc[drv, "Conso / tour (L)"]
        usable = p.tank_l - p.fuel_margin_l
        laps_fuel = int(usable // cons)
        laps_time = int((p.max_stint_min * 60) // pace) if p.max_stint_min else laps_fuel
        laps_left = int((total_s - t) // pace) + 1
        n = max(1, min(laps_fuel, laps_time, laps_left))

        stint_s = n * pace
        fuel_needed = n * cons + p.fuel_margin_l
        last = t + stint_s >= total_s
        pit = 0.0 if last else p.pit_loss_s + fuel_needed / p.refuel_rate_lps

        rows.append(
            {
                "Relais": len(rows) + 1,
                "Pilote": drv,
                "Début": _fmt(t),
                "Fin": _fmt(t + stint_s),
                "Durée (min)": round(stint_s / 60, 1),
                "Tours": n,
                "Tours cumulés": lap_no + n,
                "Carburant (L)": round(fuel_needed, 1),
                "Arrêt (s)": round(pit) if not last else "—",
                "Limite": "Carburant" if n == laps_fuel else ("Temps max" if n == laps_time else "Fin de course"),
            }
        )
        t += stint_s + pit
        lap_no += n
        i += 1
        if len(rows) > 200:  # sécurité
            break
    return pd.DataFrame(rows)


def _fmt(seconds: float) -> str:
    h, r = divmod(int(seconds), 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"
