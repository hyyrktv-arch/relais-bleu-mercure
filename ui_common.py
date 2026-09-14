"""Composants d'interface partagés par les pages (course iRacing, course league, pilotes, live, données)."""
from __future__ import annotations

import json
from datetime import datetime

import pandas as pd
import plotly.express as px
import streamlit as st

import g61
import season as SEASON
from stints import RaceParams, apply_overrides, driver_stats, fmt_lap, plan_from_sequence, suggest_sequence
from storage import get_store

# ---------------------------------------------------------------------------------------
# Session : auth, store, tours
# ---------------------------------------------------------------------------------------

def require_auth() -> str:
    """Retourne le rôle ('admin' | 'pilote') ; arrête la page si non connecté."""
    pw_admin = st.secrets.get("APP_PASSWORD")
    pw_pilot = st.secrets.get("APP_PASSWORD_PILOTE")
    if (pw_admin or pw_pilot) and not st.session_state.get("role"):
        st.title("Relais Bleu Mercure")
        typed = st.text_input("Mot de passe", type="password")
        if typed:
            if pw_admin and typed == pw_admin:
                st.session_state["role"] = "admin"
                st.rerun()
            elif pw_pilot and typed == pw_pilot:
                st.session_state["role"] = "pilote"
                st.rerun()
            else:
                st.error("Mot de passe incorrect")
        st.stop()
    return st.session_state.get("role", "admin")


@st.cache_resource
def _store():
    return get_store(st.secrets)


def store():
    return _store()


def is_demo() -> bool:
    return bool(st.session_state.get("demo", False))


@st.cache_data(ttl=60, show_spinner=False)
def _load_laps_cached(_label: str, demo: bool) -> pd.DataFrame:
    return g61.demo_laps() if demo else store().load_laps()


def load_laps() -> pd.DataFrame:
    try:
        return _load_laps_cached(store().label, is_demo())
    except Exception as e:  # noqa: BLE001
        st.error(f"Lecture de la base impossible — {e}")
        st.stop()


def refresh_laps():
    _load_laps_cached.clear()


def sidebar_common(role: str):
    with st.sidebar:
        st.markdown(f"**{'Stratège' if role == 'admin' else 'Pilote'}** · {store().label}")
        if role == "admin":
            st.toggle("Mode démo (données fictives)", key="demo")
        ctx = race_ctx()
        if ctx:
            st.caption(f"Course en préparation : {ctx.get('label', '—')}")
        if st.button("Se déconnecter", key="logout"):
            st.session_state.pop("role", None)
            st.rerun()


# ---------------------------------------------------------------------------------------
# Contexte de course : la course en préparation, partagée entre les pages
# ---------------------------------------------------------------------------------------

def race_ctx() -> dict:
    return st.session_state.get("race_ctx", {})


def set_race_ctx(ctx: dict):
    st.session_state["race_ctx"] = ctx


def match_in_laps(laps: pd.DataFrame, car: str | None, track: str | None) -> tuple[str | None, str | None]:
    """Trouve les libellés voiture/circuit de la base correspondant à ceux de la course."""
    if laps.empty:
        return None, None
    cars = laps["car"].dropna().unique().tolist()
    tracks = laps["track"].dropna().unique().tolist()
    car_m = next((c for c in cars if SEASON.norm(c) == SEASON.norm(car)), None) if car else None
    tr_m = SEASON.match_track(track, tracks) if track else []
    return car_m, (tr_m[0] if tr_m else None)


def select_laps_for_ctx(laps: pd.DataFrame, ctx: dict) -> pd.DataFrame:
    car_m, track_m = match_in_laps(laps, ctx.get("car"), ctx.get("track"))
    sel = laps
    if car_m:
        sel = sel[sel["car"] == car_m]
    if track_m:
        tracks = SEASON.match_track(ctx.get("track"), laps["track"].dropna().unique().tolist())
        sel = sel[sel["track"].isin(tracks)]
    return sel


# ---------------------------------------------------------------------------------------
# Section Pilotes (stats + ajustements) — renvoie stats
# ---------------------------------------------------------------------------------------

def pilots_section(sel: pd.DataFrame, key: str, show_chart: bool = True) -> pd.DataFrame:
    trim = st.slider("Tours lents écartés (%)", 0, 30, 10, key=f"trim_{key}",
                     help="Écarte les tours les plus lents (trafic, erreurs) du calcul du rythme.") / 100
    measured = driver_stats(sel, trim)

    with st.expander("Ajustements manuels (temps cible, conso, pilote sans données)", expanded=measured.empty):
        st.caption("Laisse une case vide pour garder la valeur mesurée. Temps au format 2:11.650 ou 131.65. "
                   "Un pilote absent des données peut être ajouté avec ses deux valeurs (utile pour un lift & coast ciblé).")
        ov_key = f"overrides:{key}"
        base_rows = [{"Pilote": p, "Temps cible": None, "Conso / tour (L)": None} for p in measured["Pilote"]] if not measured.empty else []
        if ov_key not in st.session_state:
            st.session_state[ov_key] = pd.DataFrame(base_rows, columns=["Pilote", "Temps cible", "Conso / tour (L)"])
        else:
            known = set(st.session_state[ov_key]["Pilote"])
            extra = [r for r in base_rows if r["Pilote"] not in known]
            if extra:
                st.session_state[ov_key] = pd.concat([st.session_state[ov_key], pd.DataFrame(extra)], ignore_index=True)
        edited_ov = st.data_editor(
            st.session_state[ov_key], num_rows="dynamic", hide_index=True, use_container_width=True, key=f"oved_{ov_key}",
            column_config={
                "Pilote": st.column_config.TextColumn(required=True),
                "Temps cible": st.column_config.TextColumn(help="ex : 2:12.000"),
                "Conso / tour (L)": st.column_config.NumberColumn(min_value=0.0, max_value=20.0, step=0.01, format="%.2f"),
            },
        )
        st.session_state[ov_key] = edited_ov
        if st.button("Réinitialiser les ajustements", key=f"ovreset_{ov_key}"):
            st.session_state.pop(ov_key, None)
            st.rerun()

    stats = apply_overrides(measured, st.session_state.get(ov_key))
    if stats.empty:
        st.warning("Pas assez de tours propres pour cette combinaison voiture/circuit, et aucun pilote manuel saisi.")
        return stats
    if "Source" not in stats.columns:
        stats["Source"] = "Mesuré"
    show = stats[["Pilote", "Source", "Temps cible", "Tours", "Meilleur", "Rythme moyen", "Écart-type",
                  "Conso / tour (L)", "Conso max (L)", "Tours avec conso"]]
    st.dataframe(show.style.format({"Meilleur": "{:.3f}", "Rythme moyen": "{:.3f}", "Écart-type": "{:.3f}",
                                    "Conso / tour (L)": "{:.2f}", "Conso max (L)": "{:.2f}"}),
                 hide_index=True, use_container_width=True)
    st.caption("Temps cible = rythme moyen sur tours propres après retrait des tours lents. "
               "C'est le tour que chaque pilote doit répéter pour que le plan se réalise.")
    if show_chart:
        clean = sel[sel["clean"].astype(bool)].dropna(subset=["lap_time"])
        if not clean.empty:
            fig = px.box(clean, x="driver", y="lap_time", color="driver", points="all",
                         labels={"driver": "", "lap_time": "Temps au tour (s)"}, title="Distribution des temps au tour")
            fig.update_layout(showlegend=False, height=380)
            st.plotly_chart(fig, use_container_width=True)
    return stats


# ---------------------------------------------------------------------------------------
# Section Réglages de course — renvoie dict de paramètres communs
# ---------------------------------------------------------------------------------------

def params_section(key: str, defaults: dict, stats: pd.DataFrame | None = None) -> dict:
    """defaults : duration_min, tank_l, pit_mode, pit_loss_s, refuel_rate_lps, fuel_margin_l, max_stint_min, start_fuel_l"""
    k = lambda n: f"{key}:{n}"  # noqa: E731
    for name, val in defaults.items():
        st.session_state.setdefault(k(name), val)
    a, b = st.columns(2)
    duration = a.number_input("Durée de course (min)", 30, 1500, step=30, key=k("duration_min"))
    tank = b.number_input("Réservoir (L)", 20.0, 250.0, step=1.0, key=k("tank_l"))
    pit_mode = st.radio("Temps d'arrêt", ["Durée fixe (règlement)", "Dépend du carburant ajouté"], horizontal=True, key=k("pit_mode"),
                        help="Durée fixe : chaque arrêt coûte le même temps. Dépend du carburant : perte fixe + carburant ÷ débit.")
    c, d = st.columns(2)
    if pit_mode.startswith("Durée fixe"):
        pit_loss = c.number_input("Temps perdu par arrêt, tout compris (s)", 10.0, 400.0, step=5.0, key=k("pit_loss_s"))
        refuel = None
        d.caption("Le carburant ajouté ne change pas la durée de l'arrêt.")
    else:
        pit_loss = c.number_input("Perte par arrêt hors ravitaillement (s)", 10.0, 400.0, step=5.0, key=k("pit_loss_s"))
        refuel = d.number_input("Débit ravitaillement (L/s)", 0.1, 20.0, step=0.1, key=k("refuel_rate_lps"),
                                help="Lis le temps estimé dans la boîte noire iRacing pour 100 L : 100 ÷ secondes.")
    e, f, g = st.columns(3)
    margin = e.number_input("Marge carburant par défaut (L)", 0.0, 10.0, step=0.5, key=k("fuel_margin_l"))
    max_stint = f.number_input("Relais max par défaut (min, 0 = aucun)", 0, 300, step=10, key=k("max_stint_min"))
    start_fuel = g.number_input("Carburant imposé au départ (L, 0 = libre)", 0.0, 250.0, step=1.0, key=k("start_fuel_l"))

    params = dict(duration_min=int(duration), tank_l=float(tank), pit_mode=pit_mode, pit_loss_s=float(pit_loss),
                  refuel_rate_lps=refuel, fuel_margin_l=float(margin), max_stint_min=int(max_stint),
                  start_fuel_l=float(start_fuel) or None)

    if stats is not None and not stats.empty:
        cons = stats["Conso / tour (L)"].mean()
        pace = stats["Rythme moyen"].mean()
        usable = tank - margin
        laps_per_stint = int(usable // cons)
        total_s = duration * 60
        est_laps = int(total_s // pace)
        st.markdown(f"Moyenne équipe **{cons:.2f} L/tour**, **{fmt_lap(pace)}** : un relais plein tient **{laps_per_stint} tours** "
                    f"(~{int(laps_per_stint * pace / 60)} min). Course ≈ **{est_laps} tours**.")
        # Calculateur lift & coast : conso nécessaire pour finir en N arrêts
        with st.expander("Calculateur : combien économiser pour supprimer un arrêt ?"):
            n_stops_now = max(0, -(-est_laps // laps_per_stint) - 1)
            target_stops = st.number_input("Arrêts visés", 0, 20, max(0, n_stops_now - 1), key=k("target_stops"))
            stints = target_stops + 1
            laps_per = -(-est_laps // stints)
            first_cap = start_fuel if start_fuel else tank
            need = (first_cap - margin) / laps_per if stints == 1 else (tank - margin) / laps_per
            saving = (cons - need) / cons * 100
            gain_s = (n_stops_now - target_stops) * (pit_loss + (tank / refuel if refuel else 0))
            max_loss_lap = gain_s / est_laps if est_laps else 0
            st.markdown(f"Aujourd'hui **{n_stops_now} arrêt(s)**. Pour finir en **{target_stops}**, il faut **{laps_per} tours par relais**, "
                        f"soit **{need:.2f} L/tour** ({saving:+.1f} % vs mesuré).  \n"
                        f"Gain : **{gain_s:.0f} s** d'arrêts. Le lift reste rentable jusqu'à **{max_loss_lap:.2f} s/tour** de perte.")
            if saving > 8:
                st.warning("Plus de 8 % d'économie : rarement tenable sur tout un relais sans perte de rythme importante.")
    return params


# ---------------------------------------------------------------------------------------
# Section Équipages — plans par voiture, avec sauvegarde partagée optionnelle
# ---------------------------------------------------------------------------------------

def crews_section(stats: pd.DataFrame, params: dict, key: str, event_key: str | None = None, role: str = "admin",
                  saved_plans: list[dict] | None = None):
    """event_key non None => plans partagés en base (chargement + bouton Enregistrer)."""
    if stats.empty:
        st.warning("Renseigne d'abord les pilotes (mesurés ou manuels).")
        return
    saved = {p["car_name"]: p for p in (saved_plans or [])}
    k = lambda n: f"{key}:{n}"  # noqa: E731
    default_n = max(len(saved), 3) if saved else 3
    n_crews = st.number_input("Nombre de voitures", 1, 8, default_n, key=k("n_crews"))
    pilots = stats["Pilote"].tolist()
    common = dict(duration_min=params["duration_min"], tank_l=params["tank_l"], pit_loss_s=params["pit_loss_s"],
                  refuel_rate_lps=params["refuel_rate_lps"], start_fuel_l=params["start_fuel_l"])
    summary = []
    saved_names = list(saved.keys())

    for i in range(int(n_crews)):
        st.divider()
        default_name = saved_names[i] if i < len(saved_names) else f"Bleu Mercure #{i + 1}"
        sp = saved.get(default_name)
        # préchargement du plan partagé une seule fois
        if sp and not st.session_state.get(k(f"loaded_{i}")):
            st.session_state[k(f"crew_name_{i}")] = default_name
            st.session_state[k(f"crew_drv_{i}")] = [d for d in sp.get("drivers", []) if d in pilots]
            st.session_state[k(f"seq_{i}")] = [(a, float(b)) for a, b in sp.get("sequence", [])]
            st.session_state[k(f"crew_margin_{i}")] = float(sp.get("params", {}).get("fuel_margin_l", params["fuel_margin_l"]))
            st.session_state[k(f"crew_stint_{i}")] = int(sp.get("params", {}).get("max_stint_min", params["max_stint_min"]))
            st.session_state[k(f"loaded_{i}")] = True
        h1, h2, h3, h4 = st.columns([2, 3, 1.2, 1.2])
        name = h1.text_input("Voiture", value=st.session_state.get(k(f"crew_name_{i}"), default_name), key=k(f"crew_name_{i}"))
        default_drv = pilots[2 * i: 2 * i + 2] if 2 * i < len(pilots) else []
        drivers = h2.multiselect("Pilotes (ordre d'alternance)", pilots,
                                 default=st.session_state.get(k(f"crew_drv_{i}"), default_drv), key=k(f"crew_drv_{i}"))
        crew_margin = h3.number_input("Marge (L)", 0.0, 10.0, st.session_state.get(k(f"crew_margin_{i}"), params["fuel_margin_l"]),
                                      step=0.5, key=k(f"crew_margin_{i}"))
        crew_stint = h4.number_input("Relais max (min)", 0, 300, st.session_state.get(k(f"crew_stint_{i}"), params["max_stint_min"]),
                                     step=10, key=k(f"crew_stint_{i}"))
        rp = RaceParams(**common, fuel_margin_l=crew_margin, max_stint_min=crew_stint or None, driver_order=drivers)

        locked = bool(sp and sp.get("locked"))
        if locked:
            st.info(f"Plan figé par {sp.get('updated_by') or 'le stratège'} · {str(sp.get('updated_at', ''))[:16].replace('T', ' ')} UTC")

        g1, g2, g3 = st.columns([1.5, 1, 1])
        mode = g1.radio("Proposition", ["Pleins complets", "Carburant équilibré"], horizontal=True, key=k(f"mode_{i}"))
        n_st = g2.number_input("Nombre de relais", 1, 40, 4, key=k(f"nst_{i}"), disabled=mode == "Pleins complets")
        seq_key, ver_key = k(f"seq_{i}"), k(f"seqver_{i}")
        if g3.button("Proposer la séquence", key=k(f"gen_{i}"), disabled=not drivers or (locked and role != "admin")):
            st.session_state[seq_key] = suggest_sequence(stats, rp, drivers, "plein" if mode == "Pleins complets" else "equilibre", int(n_st))
            st.session_state[ver_key] = st.session_state.get(ver_key, 0) + 1
        if seq_key not in st.session_state and drivers:
            st.session_state[seq_key] = suggest_sequence(stats, rp, drivers, "plein")
        seq = st.session_state.get(seq_key, [])
        if not drivers or not seq:
            st.info("Choisis les pilotes de cette voiture.")
            continue

        edit_df = pd.DataFrame(seq, columns=["Pilote", "Carburant embarqué (L)"])
        edit_df.insert(0, "Relais", range(1, len(edit_df) + 1))
        edited = st.data_editor(
            edit_df, hide_index=True, use_container_width=True, num_rows="dynamic",
            key=k(f"editor_{i}_{st.session_state.get(ver_key, 0)}"), disabled=locked and role != "admin",
            column_config={
                "Relais": st.column_config.NumberColumn(disabled=True),
                "Pilote": st.column_config.SelectboxColumn(options=drivers, required=True),
                "Carburant embarqué (L)": st.column_config.NumberColumn(min_value=1.0, max_value=float(params["tank_l"]), step=0.5, format="%.1f"),
            },
        )
        new_seq = [(r["Pilote"], float(r["Carburant embarqué (L)"])) for _, r in edited.iterrows()
                   if pd.notna(r["Pilote"]) and pd.notna(r["Carburant embarqué (L)"])]
        plan, cov = plan_from_sequence(stats, rp, new_seq)
        if plan.empty:
            continue
        if params["start_fuel_l"] and new_seq and abs(new_seq[0][1] - params["start_fuel_l"]) > 0.05:
            st.warning(f"Le règlement impose {params['start_fuel_l']:.0f} L au départ, le relais 1 en prévoit {new_seq[0][1]:.1f} L.")
        if cov["manque_s"] > 0:
            full = all(f >= params["tank_l"] - 0.05 for _, f in new_seq)
            hint = "ajoute un relais (tous les réservoirs sont déjà pleins)." if full else "ajoute du carburant ou un relais."
            st.error(f"La séquence s'arrête {cov['manque_s'] / 60:.1f} min avant la fin de course : {hint}")
        elif cov["trop_s"] > 60:
            st.info(f"Marge de {cov['trop_s'] / 60:.1f} min au-delà de la fin de course, le dernier relais sera raccourci.")
        else:
            st.success("La séquence couvre la course.")
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Relais", len(plan))
        m2.metric("Arrêts", len(plan) - 1)
        m3.metric("Tours estimés", int(cov["tours"]))
        m4.metric("Carburant total (L)", cov["carburant_total"])
        st.dataframe(plan, hide_index=True, use_container_width=True)

        b1, b2, b3, b4 = st.columns([1, 1, 1, 2])
        b1.download_button("Exporter (CSV)", plan.to_csv(index=False).encode(),
                           file_name=f"relais_{name.replace(' ', '_')}.csv", mime="text/csv", key=k(f"dl_{i}"))
        if event_key and role == "admin":
            lock = b2.toggle("Figer", value=locked, key=k(f"lock_{i}"), help="Un plan figé n'est modifiable que par le stratège.")
            if b3.button("Enregistrer pour l'équipe", key=k(f"save_{i}"), type="primary"):
                try:
                    store().save_plan(event_key, name, drivers, [[a, b] for a, b in new_seq],
                                      {**params, "fuel_margin_l": crew_margin, "max_stint_min": crew_stint},
                                      locked=lock, updated_by="stratège")
                    b4.success("Plan enregistré, visible par toute l'équipe.")
                except Exception as e:  # noqa: BLE001
                    b4.error(f"Sauvegarde impossible : {e}")
        elif event_key:
            b2.caption("Plan partagé (lecture).")

        per_driver = plan.groupby("Pilote")["Durée (min)"].sum()
        summary.append({
            "Voiture": name, "Pilotes": " / ".join(drivers), "Relais": len(plan), "Arrêts": len(plan) - 1,
            "Tours estimés": int(cov["tours"]), "Carburant (L)": cov["carburant_total"],
            "Temps aux stands (s)": int(pd.to_numeric(plan["Arrêt (s)"], errors="coerce").fillna(0).sum()),
            "Volant max/min (min)": f"{per_driver.max():.0f} / {per_driver.min():.0f}",
        })
    if summary:
        st.divider()
        st.subheader("Comparatif des voitures")
        st.dataframe(pd.DataFrame(summary), hide_index=True, use_container_width=True)


# ---------------------------------------------------------------------------------------
# Enchaînement Réglages -> Pilotes -> Équipages pour une course donnée
# ---------------------------------------------------------------------------------------

def race_workflow(laps: pd.DataFrame, ctx: dict, key: str, role: str, event_key: str | None = None, rules: dict | None = None):
    sel = select_laps_for_ctx(laps, ctx)
    car_m, track_m = match_in_laps(laps, ctx.get("car"), ctx.get("track"))
    if car_m is None or track_m is None:
        st.warning(f"Aucun tour en base pour **{ctx.get('car')}** à **{ctx.get('track')}**. "
                   "Séance d'essais à prévoir avec l'agent lancé, ou saisis des pilotes manuels ci-dessous.")
    else:
        st.caption(f"Données : {len(sel)} tours ({car_m}, {track_m}).")

    t_params, t_pilots, t_crews = st.tabs(["Réglages", "Pilotes", "Équipages et plan"])
    with t_pilots:
        stats = pilots_section(sel, key=f"{key}:{ctx.get('car')}:{ctx.get('track')}")
    with t_params:
        rules = rules or {}
        defaults = dict(duration_min=int(ctx.get("duration_min") or 120), tank_l=float(rules.get("tank_l", 100.0)),
                        pit_mode=rules.get("pit_mode", "Durée fixe (règlement)"), pit_loss_s=float(rules.get("pit_loss_s", 120.0)),
                        refuel_rate_lps=float(rules.get("refuel_rate_lps", 3.0)), fuel_margin_l=float(rules.get("fuel_margin_l", 2.0)),
                        max_stint_min=int(rules.get("max_stint_min", 0)), start_fuel_l=float(rules.get("start_fuel_l") or 0.0))
        params = params_section(key, defaults, stats)
    with t_crews:
        saved = []
        if event_key:
            try:
                saved = store().list_plans(event_key)
            except Exception as e:  # noqa: BLE001
                st.warning(f"Plans partagés indisponibles : {e}")
        crews_section(stats, params, key=f"{key}:crews", event_key=event_key, role=role, saved_plans=saved)
    return params
