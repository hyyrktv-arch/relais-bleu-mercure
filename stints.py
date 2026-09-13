
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
                "Temps cible": fmt_lap(kept["lap_time"].mean()),
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
    start_fuel_l: float | None = None   # carburant imposé au départ (ex : plein obligatoire) ou None
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


def parse_lap(text) -> float | None:
    """'2:11.650' ou '131.65' -> secondes. None si vide/invalide."""
    if text is None or (isinstance(text, float) and pd.isna(text)):
        return None
    t = str(text).strip().replace(",", ".")
    if not t:
        return None
    try:
        if ":" in t:
            m, sec = t.split(":", 1)
            return int(m) * 60 + float(sec)
        return float(t)
    except ValueError:
        return None


def apply_overrides(stats: pd.DataFrame, overrides: pd.DataFrame) -> pd.DataFrame:
    """Remplace rythme/conso par les valeurs saisies ; ajoute les pilotes manuels.

    overrides : colonnes Pilote, Temps cible (texte), Conso / tour (L).
    """
    if overrides is None or overrides.empty:
        return stats
    out = stats.copy()
    if "Source" not in out.columns:
        out["Source"] = "Mesuré"
    for _, r in overrides.iterrows():
        name = str(r.get("Pilote") or "").strip()
        if not name:
            continue
        pace = parse_lap(r.get("Temps cible"))
        cons = r.get("Conso / tour (L)")
        cons = None if cons is None or pd.isna(cons) or float(cons) <= 0 else float(cons)
        if pace is None and cons is None:
            continue
        if name in out["Pilote"].values:
            i = out.index[out["Pilote"] == name][0]
            if pace:
                out.loc[i, "Rythme moyen"] = pace
                out.loc[i, "Temps cible"] = fmt_lap(pace)
            if cons:
                out.loc[i, "Conso / tour (L)"] = cons
                out.loc[i, "Conso max (L)"] = max(cons, float(out.loc[i, "Conso max (L)"]) if pd.notna(out.loc[i, "Conso max (L)"]) else cons)
            out.loc[i, "Source"] = "Ajusté"
        else:
            if not (pace and cons):
                continue  # un pilote manuel a besoin des deux valeurs
            out = pd.concat([out, pd.DataFrame([{
                "Pilote": name, "Tours": 0, "Meilleur": pace, "Rythme moyen": pace, "Écart-type": None,
                "Conso / tour (L)": cons, "Conso max (L)": cons, "Tours avec conso": 0,
                "Temps cible": fmt_lap(pace), "Source": "Manuel",
            }])], ignore_index=True)
    return out.sort_values("Rythme moyen").reset_index(drop=True)


def fmt_lap(seconds: float) -> str:
    """128.743 -> 2:08.743"""
    if seconds is None or pd.isna(seconds):
        return "—"
    m, sec = divmod(float(seconds), 60)
    return f"{int(m)}:{sec:06.3f}"


def _fmt(seconds: float) -> str:
    h, r = divmod(int(seconds), 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


# --- plans à séquence explicite -------------------------------------------------

def plan_from_sequence(stats: pd.DataFrame, p: RaceParams, sequence: list[tuple[str, float]]) -> tuple[pd.DataFrame, dict]:
    """Plan à partir d'une liste (pilote, carburant embarqué) définie relais par relais.

    Le carburant du relais i est celui mis dans la voiture au départ du relais i
    (relais 1 = plein de départ). Retourne le plan et un bilan de couverture.
    """
    if stats.empty or not sequence:
        return pd.DataFrame(), {}
    s = stats.set_index("Pilote")
    total_s = p.duration_min * 60
    t, lap_no, rows = 0.0, 0, []
    for i, (drv, fuel) in enumerate(sequence):
        if drv not in s.index:
            continue
        pace, cons = s.loc[drv, "Rythme moyen"], s.loc[drv, "Conso / tour (L)"]
        fuel = min(float(fuel), p.tank_l)
        laps_fuel = max(0, int((fuel - p.fuel_margin_l) // cons))
        laps_time = int((p.max_stint_min * 60) // pace) if p.max_stint_min else laps_fuel
        laps_left = max(0, int((total_s - t) // pace) + 1)
        n = max(0, min(laps_fuel, laps_time, laps_left))
        stint_s = n * pace
        last = (i == len(sequence) - 1) or (t + stint_s >= total_s)
        pit = 0.0 if last else p.pit_loss_s + (sequence[i + 1][1] / p.refuel_rate_lps)
        limite = "Fin de course" if t + stint_s >= total_s else ("Temps max" if n == laps_time and laps_time < laps_fuel else "Carburant")
        rows.append({
            "Relais": i + 1, "Pilote": drv, "Temps cible": fmt_lap(pace), "Conso cible (L)": round(cons, 2),
            "Début": _fmt(t), "Fin": _fmt(t + stint_s),
            "Durée (min)": round(stint_s / 60, 1), "Tours": n, "Tours cumulés": lap_no + n,
            "Carburant embarqué (L)": round(fuel, 1), "Carburant restant (L)": round(fuel - n * cons, 1),
            "Arrêt (s)": round(pit) if not last else "—", "Limite": limite,
        })
        t += stint_s + pit
        lap_no += n
        if t >= total_s:
            break
    plan = pd.DataFrame(rows)
    coverage = {
        "fin_plan_s": t, "course_s": total_s, "manque_s": max(0.0, total_s - t),
        "trop_s": max(0.0, t - total_s), "tours": lap_no,
        "carburant_total": round(sum(f for _, f in sequence[: len(rows)]), 1),
    }
    return plan, coverage


def suggest_sequence(stats: pd.DataFrame, p: RaceParams, drivers: list[str],
                     mode: str = "plein", n_stints: int | None = None) -> list[tuple[str, float]]:
    """Propose une séquence (pilote, carburant).

    mode="plein"     : pleins complets, pilotes en alternance, autant de relais que nécessaire.
    mode="equilibre" : n_stints relais avec le même carburant chacun (ex. 80/80 au lieu de 100/60).
    """
    if stats.empty or not drivers:
        return []
    base = plan_stints(stats, RaceParams(**{**p.__dict__, "driver_order": drivers}))
    if base.empty:
        return []
    if mode == "plein":
        return [(r["Pilote"], p.tank_l) for _, r in base.iterrows()]
    n = n_stints or len(base)
    total_fuel = float(base["Carburant (L)"].sum())
    first = p.start_fuel_l if p.start_fuel_l else None
    s = stats.set_index("Pilote")
    pace = s.loc[drivers, "Rythme moyen"].mean()
    cons = s.loc[drivers, "Conso / tour (L)"].mean()

    def build(per: float) -> list[tuple[str, float]]:
        seq = [(drivers[i % len(drivers)], round(min(p.tank_l, per), 1)) for i in range(n)]
        if first is not None:
            seq[0] = (seq[0][0], round(first, 1))
        return seq

    rest = n - (1 if first is not None else 0)
    if rest <= 0:
        return build(first or p.tank_l)
    per = (total_fuel - (first or 0)) / rest + p.fuel_margin_l
    seq = build(per)
    # ajuste le carburant des relais libres : couvrir la course, avec au plus ~1 tour de rab
    for _ in range(20):
        _, cov = plan_from_sequence(stats, p, seq)
        manque, trop = cov.get("manque_s", 0), cov.get("trop_s", 0)
        if manque == 0 and trop <= pace:
            break
        if manque > 0:
            if per >= p.tank_l:
                break  # réservoir plein partout : il faut un relais de plus
            per = min(p.tank_l, per + max(cons / rest, (manque / pace) * cons / rest))
        else:
            per = max(p.fuel_margin_l + 1, per - ((trop - pace) / pace) * cons / rest)
        seq = build(per)
    return [(d, round(f, 1)) for d, f in seq]
