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
# Identité visuelle
# ---------------------------------------------------------------------------------------

_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&display=swap');
html, body, [class*="css"], .stApp, .stMarkdown, .stDataFrame, button, input, textarea, select {
  font-family: 'IBM Plex Sans', 'Segoe UI', Arial, sans-serif !important;
  font-variant-numeric: tabular-nums;
}
h1 { font-weight: 600 !important; letter-spacing: -0.01em; }
h2, h3 { font-weight: 600 !important; }
/* chiffres façon panneau de stand */
[data-testid="stMetricValue"] { font-size: 1.9rem !important; font-weight: 600 !important; letter-spacing: -0.01em; }
[data-testid="stMetricLabel"] { color: #8FA3C4 !important; font-size: 0.82rem !important; }
[data-testid="stMetricDelta"] { font-size: 0.85rem !important; }
/* conteneurs bordés : marine, coin doux, pas d'ombre */
[data-testid="stVerticalBlockBorderWrapper"] > div { border-color: #263553 !important; border-radius: 10px !important; background: #121D31; }
/* onglets */
button[data-baseweb="tab"] { font-weight: 500 !important; }
button[data-baseweb="tab"][aria-selected="true"] { color: #D0A870 !important; }
/* sidebar */
section[data-testid="stSidebar"] { background: #0B1220; border-right: 1px solid #1B2740; }
[data-testid="stSidebarHeader"] img, [data-testid="stLogo"] { height: 54px !important; max-height: 54px !important; width: auto !important; margin: 4px 0 6px 0; }
section[data-testid="stSidebar"] [data-testid="stSidebarNav"] a { border-radius: 8px; }
/* bandeau de course */
.rbm-band { display:flex; align-items:center; justify-content:space-between; gap:16px;
  padding: 10px 16px; margin: -8px 0 14px 0; border-radius: 10px;
  background: linear-gradient(90deg, #16223A 0%, #121D31 100%); border-left: 4px solid #D0A870; }
.rbm-band .l { color:#8FA3C4; font-size:0.78rem; }
.rbm-band .v { color:#E8EEF8; font-size:1.05rem; font-weight:600; }
.rbm-band .num { font-size:1.4rem; font-weight:600; color:#E8EEF8; }
/* dataframes : entête discrète */
[data-testid="stDataFrame"] { border-radius: 8px; overflow: hidden; }
/* boutons primaires */
.stButton > button[kind="primary"] { border-radius: 8px; font-weight: 600; }
.stButton > button { border-radius: 8px; }
/* tables de plan : moins de contraste sur les bordures */
[data-testid="stTable"] td, [data-testid="stTable"] th { border-color: #263553 !important; }
</style>
"""


def apply_branding():
    """Logo + feuille de style. À appeler une fois par exécution, avant tout rendu."""
    import os
    for cand in ("assets/logo.png", "assets/logo.svg"):
        if os.path.exists(cand):
            try:
                st.logo(cand, size="large")
            except Exception:  # noqa: BLE001
                pass
            break
    st.markdown(_CSS, unsafe_allow_html=True)


def race_band(ctx: dict | None):
    """Bandeau de la course en préparation, en tête de page."""
    if not ctx:
        return
    parts = []
    start = ctx.get("start")
    when = ""
    if start:
        try:
            s_ = pd.Timestamp(start)
            d = s_ - pd.Timestamp.now(tz=s_.tz)
            secs = d.total_seconds()
            if secs > 0:
                when = f"J-{int(secs // 86400)} · {int(secs % 86400 // 3600)} h" if secs >= 86400 else f"dans {int(secs // 3600)} h {int(secs % 3600 // 60):02d}"
            elif secs > -ctx.get("duration_min", 0) * 60:
                when = "en course"
            else:
                when = "terminée"
            parts.append(SEASON.fr(s_.tz_convert("Europe/Paris"), "%a %d/%m %H:%M"))
        except Exception:  # noqa: BLE001
            pass
    parts += [p for p in (ctx.get("car"), ctx.get("track")) if p]
    dur = ctx.get("duration_min")
    st.markdown(
        f'''<div class="rbm-band">
  <div><div class="l">Course en préparation</div><div class="v">{ctx.get("label", "")}</div>
       <div class="l">{" · ".join(parts)}</div></div>
  <div style="text-align:right"><div class="l">{"Durée" if dur else ""}</div><div class="num">{f"{dur} min" if dur else ""}</div>
       <div class="l">{when}</div></div>
</div>''', unsafe_allow_html=True)


# ---------------------------------------------------------------------------------------
# Session : auth, store, tours
# ---------------------------------------------------------------------------------------

def secret(name: str, default=None):
    """st.secrets sans planter quand aucun fichier de secrets n'existe (exécution locale)."""
    try:
        return st.secrets.get(name, default)
    except Exception:  # noqa: BLE001
        return default


class _Secrets(dict):
    def get(self, k, d=None):  # compat avec get_store(secrets)
        return secret(k, d)


def require_auth() -> str:
    """Retourne le rôle ('admin' | 'pilote') ; arrête la page si non connecté."""
    pw_admin = secret("APP_PASSWORD")
    pw_pilot = secret("APP_PASSWORD_PILOTE")
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
    return get_store(_Secrets())


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
        st.caption(f"{'Stratège' if role == 'admin' else 'Pilote'} · {store().label}")
        if role == "admin":
            st.toggle("Mode démo (données fictives)", key="demo")
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
# Arrêts mesurés par l'agent
# ---------------------------------------------------------------------------------------

def pitstops_for_ctx(ctx: dict) -> pd.DataFrame:
    try:
        ps = store().load_pitstops()
    except Exception:  # noqa: BLE001
        return pd.DataFrame()
    if ps.empty:
        return ps
    cars = [c for c in ps["car"].dropna().unique() if SEASON.norm(c) == SEASON.norm(ctx.get("car"))]
    tracks = SEASON.match_track(ctx.get("track"), ps["track"].dropna().unique().tolist()) if ctx.get("track") else []
    sub = ps[ps["car"].isin(cars)] if cars else ps.iloc[0:0]
    sub = sub[sub["track"].isin(tracks)] if tracks else sub.iloc[0:0]
    return sub


def pit_summary(ps: pd.DataFrame) -> dict | None:
    if ps.empty:
        return None
    good = ps[(ps["pitlane_s"] > 20) & (ps["pitlane_s"] < 400)]
    if good.empty:
        return None
    refuels = good[good["refuel_rate"].notna() & (good["fuel_added"] > 5)]
    return {
        "n": int(len(good)),
        "pitlane_s": float(good["pitlane_s"].median()),
        "stationary_s": float(good["stationary_s"].median()) if good["stationary_s"].notna().any() else None,
        "refuel_rate": float(refuels["refuel_rate"].median()) if not refuels.empty else None,
        "last": good["entered_at"].max(),
    }


# ---------------------------------------------------------------------------------------
# Section Réglages de course — renvoie dict de paramètres communs
# ---------------------------------------------------------------------------------------

def params_section(key: str, defaults: dict, stats: pd.DataFrame | None = None, ctx: dict | None = None) -> dict:
    """defaults : duration_min, tank_l, pit_mode, pit_loss_s, refuel_rate_lps, fuel_margin_l, max_stint_min, start_fuel_l"""
    k = lambda n: f"{key}:{n}"  # noqa: E731
    for name, val in defaults.items():
        st.session_state.setdefault(k(name), val)

    # arrêts mesurés par l'agent sur cette voiture / ce circuit
    if ctx:
        summ = pit_summary(pitstops_for_ctx(ctx))
        if summ:
            with st.container(border=True):
                c1, c2 = st.columns([3, 1])
                txt = f"**{summ['n']} arrêt(s) mesuré(s)** par l'agent ici : pit lane {summ['pitlane_s']:.0f} s"
                if summ["stationary_s"]:
                    txt += f", arrêt {summ['stationary_s']:.0f} s"
                if summ["refuel_rate"]:
                    txt += f", débit {summ['refuel_rate']:.2f} L/s"
                c1.markdown(txt)
                c1.caption("La pit lane inclut le temps d'arrêt. La perte réelle par rapport à un tour normal est un peu inférieure "
                           "(la portion de pit lane remplace une partie du tour).")
                if c2.button("Appliquer", key=k("apply_pits")):
                    st.session_state[k("pit_loss_s")] = round(summ["pitlane_s"] - (summ["stationary_s"] or 0) + (summ["stationary_s"] or 0), 0) \
                        if summ["refuel_rate"] is None else round(summ["pitlane_s"] - (summ["stationary_s"] or 0), 0) + 5.0
                    if summ["refuel_rate"]:
                        st.session_state[k("pit_mode")] = "Dépend du carburant ajouté"
                        st.session_state[k("refuel_rate_lps")] = round(summ["refuel_rate"], 2)
                    else:
                        st.session_state[k("pit_mode")] = "Durée fixe (règlement)"
                    st.rerun()
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
            target_stops = st.number_input("Arrêts visés", 0, max(60, n_stops_now + 5), min(max(0, n_stops_now - 1), max(60, n_stops_now + 5)),
                                           key=k("target_stops"))
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
        params = params_section(key, defaults, stats, ctx)
    with t_crews:
        saved = []
        if event_key:
            try:
                saved = store().list_plans(event_key)
            except Exception as e:  # noqa: BLE001
                st.warning(f"Plans partagés indisponibles : {e}")
        crews_section(stats, params, key=f"{key}:crews", event_key=event_key, role=role, saved_plans=saved)
    return params


# ---------------------------------------------------------------------------------------
# Suivi en course : écart au plan (utilisé par la page En piste)
# ---------------------------------------------------------------------------------------

def race_tracking(ctx: dict, laps: pd.DataFrame, live: pd.DataFrame, recent: pd.DataFrame, role: str):
    """Compare la course en cours (live + tours récents) au plan partagé de chaque voiture."""
    if not ctx.get("start") or not ctx.get("event_key"):
        return
    start = pd.Timestamp(ctx["start"])
    now = pd.Timestamp.now(tz="UTC")
    elapsed = (now - start).total_seconds()
    total = ctx.get("duration_min", 0) * 60
    if elapsed < -3600 * 6 or elapsed > total + 1800:
        return  # trop loin de la course
    try:
        plans = store().list_plans(ctx["event_key"])
    except Exception:  # noqa: BLE001
        plans = []
    if not plans:
        st.info("Aucun plan enregistré pour cette course : enregistre les équipages dans la page de la course pour suivre l'écart au plan.")
        return

    sel = select_laps_for_ctx(laps, ctx)
    stats = driver_stats(sel) if not sel.empty else pd.DataFrame()
    st.subheader(f"Suivi de course — {ctx.get('label')}")
    if 0 <= elapsed <= total:
        st.progress(min(1.0, elapsed / total), text=f"{int(elapsed // 60)} / {ctx['duration_min']} min écoulées")
    elif elapsed < 0:
        st.caption(f"Départ dans {int(-elapsed // 3600)} h {int(-elapsed % 3600 // 60)} min — le plan s'affiche, le suivi démarre au départ.")

    for p in plans:
        seq = [(a, float(b)) for a, b in p.get("sequence", [])]
        pr = p.get("params", {})
        rp = RaceParams(duration_min=ctx["duration_min"], tank_l=float(pr.get("tank_l", 100)), pit_loss_s=float(pr.get("pit_loss_s", 120)),
                        refuel_rate_lps=pr.get("refuel_rate_lps"), fuel_margin_l=float(pr.get("fuel_margin_l", 2)),
                        max_stint_min=pr.get("max_stint_min") or None, start_fuel_l=pr.get("start_fuel_l"), driver_order=p.get("drivers", []))
        # stats pour les pilotes du plan (mesurées, ou fallback rythme/conso génériques)
        st_ = stats.copy() if not stats.empty else pd.DataFrame()
        missing = [d for d in dict.fromkeys(a for a, _ in seq) if st_.empty or d not in st_["Pilote"].values]
        if missing:
            base_pace = st_["Rythme moyen"].mean() if not st_.empty else 130.0
            base_cons = st_["Conso / tour (L)"].mean() if not st_.empty else 3.5
            st_ = pd.concat([st_, pd.DataFrame([{"Pilote": d, "Rythme moyen": base_pace, "Conso / tour (L)": base_cons, "Conso max (L)": base_cons,
                                                  "Tours": 0, "Meilleur": base_pace, "Écart-type": None, "Tours avec conso": 0, "Temps cible": fmt_lap(base_pace)}
                                                 for d in missing])], ignore_index=True)
        plan, cov = plan_from_sequence(st_, rp, seq)
        if plan.empty:
            continue
        with st.container(border=True):
            st.markdown(f"### {p['car_name']}" + (" 🔒" if p.get("locked") else ""))
            # relais prévu à l'instant t
            def _sec(hms):
                h, m, s_ = hms.split(":")
                return int(h) * 3600 + int(m) * 60 + int(s_)
            plan["_deb"] = plan["Début"].apply(_sec)
            plan["_fin"] = plan["Fin"].apply(_sec)
            cur = plan[(plan["_deb"] <= max(elapsed, 0)) & (plan["_fin"] >= max(elapsed, 0))]
            cur = cur.iloc[0] if not cur.empty else (plan.iloc[0] if elapsed < 0 else plan.iloc[-1])
            nxt_stops = plan[plan["_fin"] > elapsed]
            # pilote réellement en piste (live) parmi les pilotes de ce plan
            drivers_plan = list(dict.fromkeys(a for a, _ in seq))
            lv = live[live["driver"].isin(drivers_plan)] if not live.empty else pd.DataFrame()
            lv = lv.sort_values("updated_at", ascending=False) if not lv.empty else lv
            actual = lv.iloc[0] if not lv.empty else None

            c = st.columns(5)
            c[0].metric("Relais prévu", f"{int(cur['Relais'])} / {len(plan)}", cur["Pilote"])
            if actual is not None:
                same = actual["driver"] == cur["Pilote"]
                c[1].metric("Au volant", actual["driver"], "conforme" if same else "≠ plan", delta_color="normal" if same else "inverse")
                c[2].metric("Carburant", f"{actual['fuel_level']:.1f} L" if pd.notna(actual.get("fuel_level")) else "—")
            else:
                c[1].metric("Au volant", "—", "pas de live")
            # tours prévus à cet instant vs réels
            laps_planned = None
            if elapsed >= 0:
                done_before = plan[plan["_fin"] < elapsed]["Tours"].sum()
                pace_cur = float(st_.set_index("Pilote").loc[cur["Pilote"], "Rythme moyen"]) if cur["Pilote"] in st_["Pilote"].values else 130.0
                in_stint = max(0, int((elapsed - cur["_deb"]) // pace_cur))
                laps_planned = int(done_before + min(in_stint, cur["Tours"]))
            if actual is not None and pd.notna(actual.get("lap")) and laps_planned is not None:
                diff = int(actual["lap"]) - laps_planned
                c[3].metric("Tours", f"{int(actual['lap'])} (prévu {laps_planned})", f"{diff:+d}", delta_color="normal" if diff >= 0 else "inverse")
            # conso réelle vs cible sur les derniers tours du pilote au volant
            if actual is not None and not recent.empty:
                mine = recent[(recent["driver"] == actual["driver"]) & recent["clean"].astype(bool)].sort_values("start_time", ascending=False).head(5)
                if len(mine) >= 2 and mine["fuel_used"].notna().any():
                    real_c = float(mine["fuel_used"].mean())
                    tgt_c = float(cur["Conso cible (L)"])
                    dc = (real_c - tgt_c) / tgt_c * 100 if tgt_c else 0
                    c[4].metric("Conso 5 tours", f"{real_c:.2f} L", f"{dc:+.1f} % vs cible", delta_color="inverse")
                    if pd.notna(actual.get("fuel_level")):
                        laps_left_fuel = int(max(0.0, actual["fuel_level"] - rp.fuel_margin_l) // real_c)
                        planned_left = int(cur["Tours"]) - (int(actual["lap"]) - int(plan[plan["_fin"] < elapsed]["Tours"].sum())) if pd.notna(actual.get("lap")) else None
                        msg = f"Autonomie réelle : **{laps_left_fuel} tours** avant marge ({fmt_lap(real_c*0+float(st_.set_index('Pilote').loc[actual['driver'],'Rythme moyen'])) if actual['driver'] in st_['Pilote'].values else ''} de rythme)"
                        if planned_left is not None:
                            msg += f" · il reste **{planned_left} tours** prévus dans ce relais"
                            if laps_left_fuel < planned_left:
                                st.error(msg + f" — **manque {planned_left - laps_left_fuel} tour(s)** : lever le pied ou avancer l'arrêt.")
                            elif laps_left_fuel - planned_left >= 2:
                                st.success(msg + f" — {laps_left_fuel - planned_left} tour(s) de rab : possibilité de rallonger le relais.")
                            else:
                                st.info(msg)
                    if dc > 3:
                        st.warning(f"Conso {dc:+.1f} % au-dessus de la cible ({tgt_c:.2f} L) : consigne économie.")
            if not nxt_stops.empty and len(nxt_stops) > 1:
                n1 = nxt_stops.iloc[0]
                st.caption(f"Prochain arrêt prévu à **{n1['Fin']}** ({n1['Pilote']} → {nxt_stops.iloc[1]['Pilote']}, "
                           f"embarquer {nxt_stops.iloc[1]['Carburant embarqué (L)']} L)")
            with st.expander("Plan complet"):
                st.dataframe(plan.drop(columns=["_deb", "_fin"]), hide_index=True, use_container_width=True)
