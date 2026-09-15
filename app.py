"""Relais Bleu Mercure — planificateur de relais iRacing.

Structure : deux modes de course (Week-end iRacing, Course league) + pages partagées (Pilotes, En piste, Données).
"""
import json
from datetime import datetime, time, timedelta

import pandas as pd
import streamlit as st

import g61
import season as SEASON
import ui_common as ui
from stints import fmt_lap

st.set_page_config(page_title="Relais Bleu Mercure", page_icon="assets/logo.svg" if __import__("os").path.exists("assets/logo.svg") else "🏁", layout="wide")
ui.apply_branding()
role = ui.require_auth()
is_admin = role == "admin"
ui.sidebar_common(role)
laps = ui.load_laps()


# =====================================================================================
# Page : Week-end iRacing
# =====================================================================================
def page_iracing():
    st.title("Week-end iRacing")
    ui.race_band(ui.race_ctx())
    season_data = SEASON.load_season()
    if not season_data["series"]:
        st.info("Aucun calendrier chargé (season.json manquant).")
        return
    cal = SEASON.upcoming(season_data)
    show_past = st.toggle("Afficher les courses passées", value=False, key="ir_past")
    if not show_past:
        cal = cal[cal["Prochain départ"].notna()]
    if cal.empty:
        st.info("Plus de course à venir cette saison.")
        return

    # --- 1. choix de la course -----------------------------------------------------
    st.subheader("1 · La course")
    labels = [f"S{r['Semaine']} · {r['Date']} · {r['Série']} · {r['Circuit']} ({r['Voiture']}, {r['Durée (min)']} min)" for _, r in cal.iterrows()]
    idx = st.selectbox("Course", range(len(labels)), format_func=lambda i: labels[i], key="ir_race_idx")
    r = cal.iloc[idx]
    race, ser = r["_race"], r["_series"]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Durée", f"{r['Durée (min)']} min")
    c2.metric("Météo", r["Météo"])
    c3.metric("Heure sim départ", r["Heure sim"][-5:] if r["Heure sim"] else "—")
    c4.metric("Format", "Team racing" if r["Team"] else "Solo")
    if race.get("fuel_limits"):
        st.caption("Limites carburant : " + ", ".join(f"{k} {v}%" for k, v in race["fuel_limits"].items()))
    ready = SEASON.readiness(laps, r["Voiture"], r["Circuit"])
    m = st.columns(4)
    m[0].metric("Tours propres en base", ready["laps"])
    m[1].metric("Pilotes ayant roulé", len(ready["drivers"]))
    m[2].metric("Conso moyenne", f"{ready['conso']:.2f} L" if ready["conso"] else "—")
    m[3].metric("Rythme moyen", fmt_lap(ready["pace"]) if ready["pace"] else "—")

    # --- 2. créneau ------------------------------------------------------------------
    st.subheader("2 · Le créneau")
    slots = r["_slots"]
    slot_labels = [SEASON.fr(d, "%A %d/%m à %H:%M") for d in slots]
    now = pd.Timestamp.now(tz="Europe/Paris").to_pydatetime()
    default_slot = next((i for i, d in enumerate(slots) if d > now), 0)
    s_idx = st.radio("Départ (heure de Paris)", range(len(slot_labels)), format_func=lambda i: slot_labels[i],
                     index=default_slot, horizontal=True, key=f"ir_slot_{idx}")
    start = slots[s_idx]
    end = start + timedelta(minutes=int(r["Durée (min)"] or 0))
    delta = start - now
    st.markdown(f"Départ **{SEASON.fr(start, '%A %d/%m %H:%M')}**, arrivée vers **{end.strftime('%H:%M')}** "
                + (f"· dans {delta.days} j {delta.seconds // 3600} h" if delta.total_seconds() > 0 else "· passé"))

    event_key = f"iracing:{ser['series']}:{race['week']}"
    ctx = {"mode": "iracing", "label": f"{r['Série']} S{r['Semaine']} {r['Circuit']}", "car": r["Voiture"],
           "track": r["Circuit"], "duration_min": int(r["Durée (min)"] or 120), "start": start.isoformat(), "event_key": event_key}
    ui.set_race_ctx(ctx)

    # --- 3/4. réglages, pilotes, plan ------------------------------------------------
    st.subheader("3 · Préparation")
    ui.race_workflow(laps, ctx, key=f"ir:{event_key}", role=role, event_key=event_key)


# =====================================================================================
# Page : Course league
# =====================================================================================
def page_league():
    st.title("Course league")
    ui.race_band(ui.race_ctx())
    store = ui.store()
    try:
        events = store.list_events()
    except Exception as e:  # noqa: BLE001
        st.error(f"Courses league indisponibles : {e}")
        return

    st.subheader("1 · La course")
    options = ["➕ Nouvelle course"] + [f"{row['name']} · {row['car']} · {row['track']} · "
                                        f"{row['race_date'].tz_convert('Europe/Paris').strftime('%d/%m %H:%M') if pd.notna(row['race_date']) else 'date ?'}"
                                        for _, row in events.iterrows()]
    if "lg_pending_choice" in st.session_state:
        pend = st.session_state.pop("lg_pending_choice")
        if pend is not None:
            ids = events["id"].tolist() if not events.empty else []
            st.session_state["lg_choice"] = (ids.index(pend) + 1) if pend in ids else 0
    choice = st.selectbox("Course", range(len(options)), format_func=lambda i: options[i], key="lg_choice")
    ev = None if choice == 0 else events.iloc[choice - 1].to_dict()

    cars_known = sorted(laps["car"].dropna().unique().tolist()) if not laps.empty else []
    tracks_known = sorted(laps["track"].dropna().unique().tolist()) if not laps.empty else []
    rules = (ev or {}).get("rules") or {}
    if isinstance(rules, str):
        rules = json.loads(rules)

    with st.form("league_form", border=True):
        f1, f2 = st.columns([2, 1])
        name = f1.text_input("Nom (league, manche)", value=(ev or {}).get("name", ""))
        dur = f2.number_input("Durée (min)", 30, 1500, int((ev or {}).get("duration_min") or 180), step=15)
        d1, d2 = st.columns(2)
        dt_default = ev["race_date"].tz_convert("Europe/Paris") if ev is not None and pd.notna(ev.get("race_date")) else None
        rdate = d1.date_input("Date", value=dt_default.date() if dt_default is not None else datetime.now().date())
        rtime = d2.time_input("Heure de départ (Paris)", value=dt_default.time() if dt_default is not None else time(20, 0))
        g1, g2 = st.columns(2)
        car_mode = g1.radio("Voiture", ["Dans la base", "Saisie libre"], horizontal=True, index=0 if (ev or {}).get("car") in cars_known or not ev else 1)
        car_val = (g1.selectbox("Voiture connue", cars_known, index=cars_known.index(ev["car"]) if ev and ev.get("car") in cars_known else 0)
                   if car_mode == "Dans la base" and cars_known else g1.text_input("Voiture", value=(ev or {}).get("car", "")))
        tr_mode = g2.radio("Circuit", ["Dans la base", "Saisie libre"], horizontal=True, index=0 if (ev or {}).get("track") in tracks_known or not ev else 1)
        tr_val = (g2.selectbox("Circuit connu", tracks_known, index=tracks_known.index(ev["track"]) if ev and ev.get("track") in tracks_known else 0)
                  if tr_mode == "Dans la base" and tracks_known else g2.text_input("Circuit", value=(ev or {}).get("track", "")))
        st.markdown("**Règlement**")
        r1, r2, r3, r4 = st.columns(4)
        tank = r1.number_input("Réservoir (L)", 20.0, 250.0, float(rules.get("tank_l", 100.0)), step=1.0)
        start_fuel = r2.number_input("Carburant imposé au départ (L, 0 = libre)", 0.0, 250.0, float(rules.get("start_fuel_l") or 0.0), step=1.0)
        pit_fixed = r3.number_input("Arrêt fixe (s, 0 = dépend du carburant)", 0.0, 400.0, float(rules.get("pit_loss_s", 120.0)) if rules.get("pit_mode", "Durée fixe (règlement)").startswith("Durée") else 0.0, step=5.0)
        max_stint = r4.number_input("Relais max (min, 0 = aucun)", 0, 300, int(rules.get("max_stint_min", 0)), step=10)
        r5, r6, r7 = st.columns(3)
        pit_loss_var = r5.number_input("Perte hors ravitaillement (s)", 10.0, 400.0, float(rules.get("pit_loss_s", 60.0)) if not rules.get("pit_mode", "Durée").startswith("Durée") else 60.0, step=5.0)
        refuel = r6.number_input("Débit ravitaillement (L/s)", 0.1, 20.0, float(rules.get("refuel_rate_lps", 3.0)), step=0.1)
        margin = r7.number_input("Marge carburant (L)", 0.0, 10.0, float(rules.get("fuel_margin_l", 2.0)), step=0.5)
        notes = st.text_area("Notes (consignes, liens, particularités)", value=rules.get("notes", ""), height=70)
        s1, s2 = st.columns([1, 4])
        submitted = s1.form_submit_button("Enregistrer la course", type="primary", disabled=not is_admin)
        if not is_admin:
            s2.caption("Seul le stratège peut créer ou modifier une course league.")

    if submitted:
        if not name.strip():
            st.error("Donne un nom à la course.")
        else:
            start_dt = pd.Timestamp(datetime.combine(rdate, rtime)).tz_localize("Europe/Paris")
            new_rules = {"tank_l": tank, "start_fuel_l": start_fuel or None, "max_stint_min": max_stint, "fuel_margin_l": margin,
                         "pit_mode": "Durée fixe (règlement)" if pit_fixed > 0 else "Dépend du carburant ajouté",
                         "pit_loss_s": pit_fixed if pit_fixed > 0 else pit_loss_var, "refuel_rate_lps": refuel, "notes": notes}
            payload = {"id": (ev or {}).get("id"), "name": name.strip(), "race_date": start_dt.tz_convert("UTC").isoformat(),
                       "car": car_val, "track": tr_val, "duration_min": int(dur), "rules": new_rules}
            try:
                saved = store.save_event(payload)
                st.session_state["lg_pending_choice"] = saved.get("id")
                st.toast(f"Course « {saved.get('name', name)} » enregistrée.")
                st.rerun()
            except Exception as e:  # noqa: BLE001
                st.error(f"Enregistrement impossible : {e}")

    if ev is None:
        st.info("Sélectionne une course existante ou enregistre-en une nouvelle pour préparer les relais.")
        return

    if is_admin:
        with st.expander("Supprimer cette course"):
            if st.checkbox("Je confirme la suppression de la course et de ses plans", key="lg_del_confirm") and st.button("Supprimer", key="lg_del"):
                store.delete_event(int(ev["id"]))
                st.session_state["lg_pending_choice"] = None
                st.rerun()

    start = ev["race_date"].tz_convert("Europe/Paris") if pd.notna(ev.get("race_date")) else None
    if start is not None:
        delta = start.to_pydatetime() - pd.Timestamp.now(tz="Europe/Paris").to_pydatetime()
        st.markdown(f"Départ **{SEASON.fr(start, '%A %d/%m %H:%M')}**, arrivée vers **{(start + pd.Timedelta(minutes=int(ev['duration_min']))).strftime('%H:%M')}** "
                    + (f"· dans {delta.days} j {delta.seconds // 3600} h" if delta.total_seconds() > 0 else "· passée"))
    if rules.get("notes"):
        st.caption(rules["notes"])

    event_key = f"league:{ev['id']}"
    ctx = {"mode": "league", "label": ev["name"], "car": ev.get("car"), "track": ev.get("track"),
           "duration_min": int(ev.get("duration_min") or 120), "start": start.isoformat() if start is not None else None, "event_key": event_key}
    ui.set_race_ctx(ctx)
    st.subheader("2 · Préparation")
    ui.race_workflow(laps, ctx, key=f"lg:{event_key}", role=role, event_key=event_key, rules=rules)


# =====================================================================================
# Page : Événements spéciaux
# =====================================================================================
def page_special():
    st.title("Événements spéciaux")
    ui.race_band(ui.race_ctx())
    import os
    if not os.path.exists("special_events.json"):
        st.info("Aucun fichier special_events.json.")
        return
    data = json.load(open("special_events.json", encoding="utf-8"))
    team_cars = data.get("team_cars", [])
    today = pd.Timestamp.now(tz="Europe/Paris").date()
    show_past = st.toggle("Afficher les événements passés", value=False, key="se_past")
    events = [e for e in data["events"] if show_past or pd.Timestamp(e["end_date"]).date() >= today]
    events.sort(key=lambda e: e["start_date"])
    st.caption(f"Source : calendrier officiel iRacing (mis à jour le {data.get('updated', '?')}). "
               f"Les créneaux exacts sont publiés sur le forum iRacing quelques jours avant chaque événement.")

    fit = [e for e in events if any(c in team_cars for c in e.get("cars", []))]
    others = [e for e in events if e not in fit]

    st.subheader("Avec nos voitures")
    if not fit:
        st.info("Aucun événement à venir avec la Ligier JS P320 ou la Dallara P217.")
    for e in fit:
        with st.container(border=True):
            h1, h2 = st.columns([3, 2])
            d1, d2 = pd.Timestamp(e["start_date"]), pd.Timestamp(e["end_date"])
            h1.markdown(f"### {e['name']}  \n{e['track']} · {e['classes']}")
            delta = (d1.date() - today).days
            h2.markdown(f"**{SEASON.fr(d1, '%d/%m')} → {SEASON.fr(d2, '%d/%m/%Y')}**  \n"
                        f"{e['duration_min'] // 60} h · team racing · " + (f"J-{delta}" if delta >= 0 else "en cours / passé"))
            st.caption(f"Nos voitures : {', '.join(c for c in e['cars'] if c in team_cars)}" + (f" · {e['notes']}" if e.get("notes") else ""))
            for car in [c for c in e["cars"] if c in team_cars]:
                ready = SEASON.readiness(laps, car, e["track"])
                m = st.columns(4)
                m[0].metric(f"Tours propres ({car.split()[0]})", ready["laps"])
                m[1].metric("Pilotes ayant roulé", len(ready["drivers"]))
                m[2].metric("Conso moyenne", f"{ready['conso']:.2f} L" if ready["conso"] else "—")
                m[3].metric("Rythme moyen", fmt_lap(ready["pace"]) if ready["pace"] else "—")
            st.link_button("Infos et créneaux (forum iRacing)", e.get("info_url", "https://forums.iracing.com/categories/special-events"))
            st.markdown("**Préparer**")
            c1, c2, c3 = st.columns([1.2, 1, 1])
            car_sel = c1.selectbox("Voiture", [c for c in e["cars"] if c in team_cars], key=f"se_car_{e['name']}")
            d_sel = c2.date_input("Jour du départ", value=d1.date() if delta >= 0 else today, min_value=d1.date(), max_value=d2.date(), key=f"se_date_{e['name']}")
            t_sel = c3.time_input("Heure de départ (Paris)", value=time(20, 0), key=f"se_time_{e['name']}")
            start = pd.Timestamp(datetime.combine(d_sel, t_sel)).tz_localize("Europe/Paris")
            event_key = f"special:{e['name']}:{e['start_date']}"
            ctx = {"mode": "special", "label": f"{e['name']} ({car_sel})", "car": car_sel, "track": e["track"],
                   "duration_min": int(e["duration_min"]), "start": start.isoformat(), "event_key": event_key}
            if st.button("Préparer cet événement", key=f"se_prep_{e['name']}", type="primary"):
                ui.set_race_ctx(ctx)
                st.rerun()
            if ui.race_ctx().get("event_key") == event_key:
                st.divider()
                ui.race_workflow(laps, ui.race_ctx(), key=f"se:{event_key}", role=role, event_key=event_key)

    if others:
        st.subheader("Autres événements team racing")
        rows = [{"Événement": e["name"], "Dates": f"{SEASON.fr(pd.Timestamp(e['start_date']), '%d/%m')} → {SEASON.fr(pd.Timestamp(e['end_date']), '%d/%m')}",
                 "Circuit": e["track"], "Durée": f"{e['duration_min'] // 60} h", "Voitures": e["classes"]} for e in others]
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
        st.caption("Pas de LMP2/LMP3 engagées : affichés pour information (un pilote peut y participer en GT3 par exemple).")


# =====================================================================================
# Page : Pilotes (exploration libre)
# =====================================================================================
def page_pilots():
    st.title("Pilotes")
    ui.race_band(ui.race_ctx())
    if laps.empty:
        st.info("Aucun tour en base.")
        return
    ctx = ui.race_ctx()
    c1, c2, c3 = st.columns(3)
    cars = sorted(laps["car"].dropna().unique())
    car_m, track_m = ui.match_in_laps(laps, ctx.get("car"), ctx.get("track")) if ctx else (None, None)
    car = c1.selectbox("Voiture", cars, index=cars.index(car_m) if car_m in cars else 0)
    tracks = sorted(laps.loc[laps["car"] == car, "track"].dropna().unique())
    track = c2.selectbox("Circuit", tracks, index=tracks.index(track_m) if track_m in tracks else 0)
    sess_all = sorted(laps["session_type"].dropna().unique())
    sessions = c3.multiselect("Type de session", sess_all, default=sess_all)
    sel = laps[(laps["car"] == car) & (laps["track"] == track) & laps["session_type"].isin(sessions)]
    ui.pilots_section(sel, key=f"free:{car}:{track}")


# =====================================================================================
# Page : En piste (live)
# =====================================================================================
def page_live():
    st.title("En piste")
    ui.race_band(ui.race_ctx())
    store = ui.store()
    c1, c2 = st.columns([3, 1])
    c1.caption("Pilotes dont l'agent envoie des données. Rafraîchissement toutes les 30 s.")
    auto = c2.toggle("Auto-rafraîchir (30 s)", value=True, key="live_auto")

    @st.fragment(run_every="30s" if auto else None)
    def render():
        if st.button("Rafraîchir maintenant", key="live_refresh"):
            pass
        live = pd.DataFrame() if ui.is_demo() else store.load_live()
        recent = pd.DataFrame() if ui.is_demo() else store.load_recent_laps(hours=12)
        now = pd.Timestamp.now(tz="UTC")
        ctx = ui.race_ctx()
        if ctx:
            ui.race_tracking(ctx, laps, live, recent, role)
            st.divider()
        if live.empty:
            st.info("Aucun pilote en piste. Ils apparaissent ici dès que leur agent est lancé et qu'ils entrent en session.")
        else:
            live["age_s"] = (now - live["updated_at"]).dt.total_seconds()
            for _, r in live.sort_values("age_s").iterrows():
                online = r["age_s"] < 60
                status = "🟢 en session" if online else ("🟡 en pause" if r["age_s"] < 900 else "⚫ hors ligne")
                since = f"il y a {int(r['age_s'])} s" if r["age_s"] < 120 else f"il y a {int(r['age_s'] // 60)} min"
                with st.container(border=True):
                    h1, h2 = st.columns([3, 2])
                    h1.markdown(f"**{r['driver']}** — {status} · {since}")
                    h2.markdown(f"{r.get('car') or ''} · {r.get('track') or ''} · {r.get('session_type') or ''}")
                    m = st.columns(6)
                    m[0].metric("Tour", int(r["lap"]) if pd.notna(r.get("lap")) else "—")
                    m[1].metric("Carburant", f"{r['fuel_level']:.1f} L" if pd.notna(r.get("fuel_level")) else "—")
                    llt = r.get("last_lap_time")
                    m[2].metric("Dernier tour", fmt_lap(llt) if pd.notna(llt) and llt > 0 else "—")
                    tr = r.get("time_remain")
                    m[3].metric("Temps restant", f"{int(tr // 3600):d}:{int(tr % 3600 // 60):02d}" if pd.notna(tr) and 0 < tr < 1e6 else "—")
                    m[4].metric("Position", int(r["position"]) if pd.notna(r.get("position")) else "—")
                    m[5].metric("Incidents", int(r["incidents"]) if pd.notna(r.get("incidents")) else "—")
                    meteo = []
                    if pd.notna(r.get("track_temp")):
                        meteo.append(f"piste {r['track_temp']:.0f}°C")
                    if pd.notna(r.get("air_temp")):
                        meteo.append(f"air {r['air_temp']:.0f}°C")
                    if r.get("track_state"):
                        meteo.append(str(r["track_state"]))
                    if r.get("on_pit_road"):
                        meteo.append("aux stands")
                    if meteo:
                        st.caption(" · ".join(meteo))
                    mine = recent[recent["driver"] == r["driver"]].sort_values("start_time", ascending=False).head(12) if not recent.empty else pd.DataFrame()
                    if not mine.empty:
                        show = mine[["start_time", "session_type", "lap_time", "fuel_used", "clean"]].copy()
                        show["Heure"] = show["start_time"].dt.tz_convert("Europe/Paris").dt.strftime("%H:%M")
                        show["Temps"] = show["lap_time"].apply(fmt_lap)
                        show["Conso (L)"] = show["fuel_used"].round(2)
                        show["Propre"] = show["clean"].map({True: "✓", False: "✗"})
                        st.dataframe(show[["Heure", "session_type", "Temps", "Conso (L)", "Propre"]].rename(columns={"session_type": "Session"}),
                                     hide_index=True, use_container_width=True, height=min(38 * (len(show) + 1), 300))
                        cl = mine[mine["clean"].astype(bool)]
                        if len(cl) >= 3:
                            st.caption(f"Sur ces {len(cl)} tours propres : moyenne {fmt_lap(cl['lap_time'].mean())}, conso {cl['fuel_used'].mean():.2f} L/tour")
        if not recent.empty:
            st.divider()
            st.subheader("Activité des 12 dernières heures")
            act = recent.groupby("driver").agg(Tours=("lap_id", "count"), Propres=("clean", "sum"), Dernier=("start_time", "max"),
                                               Voiture=("car", "last"), Circuit=("track", "last")).reset_index()
            act["Dernier"] = act["Dernier"].dt.tz_convert("Europe/Paris").dt.strftime("%d/%m %H:%M")
            act["Propres"] = act["Propres"].astype(int)
            st.dataframe(act.rename(columns={"driver": "Pilote"}).sort_values("Dernier", ascending=False), hide_index=True, use_container_width=True)

    render()


# =====================================================================================
# Page : Données (imports, tours bruts, base)
# =====================================================================================
def page_data():
    st.title("Données")
    ui.race_band(ui.race_ctx())
    store = ui.store()
    g61_enabled = str(ui.secret("G61_ENABLED", "true")).lower() not in ("false", "0", "non", "no")
    token = ui.secret("G61_TOKEN") if g61_enabled else None
    team_slug = ui.secret("G61_TEAM_SLUG")

    st.subheader("Sources")
    st.markdown(f"- **Agent iRacing** : automatique, chaque tour bouclé arrive en base.  \n- **Stockage** : {store.label}."
                + ("  \n- **Garage 61** : import manuel d'historique ci-dessous." if g61_enabled else "  \n- Garage 61 désactivé (G61_ENABLED = false)."))

    if is_admin and g61_enabled and not ui.is_demo():
        with st.expander("Import Garage 61 (historique)", expanded=False):
            if not token:
                st.error("Ajoute G61_TOKEN dans les Secrets.")
            age = st.number_input("Historique (jours)", 7, 365, 60)
            sess = st.multiselect("Sessions", ["Practice", "Qualifying", "Race"], default=["Practice", "Race"])
            sess_ids = [k for k, v in g61.SESSION_TYPES.items() if v in sess]

            @st.cache_data(ttl=3600, show_spinner="Chargement des circuits et voitures…")
            def catalogs(tok: str):
                c = g61.G61Client(tok)
                return c.tracks(), c.cars()

            tracks_df, cars_df = pd.DataFrame(), pd.DataFrame()
            if token:
                try:
                    tracks_df, cars_df = catalogs(token)
                except Exception as e:  # noqa: BLE001
                    st.error(f"Catalogue indisponible : {e}")
            track_opts = tracks_df["name"].tolist() if "name" in tracks_df.columns else []
            car_opts = cars_df["name"].tolist() if "name" in cars_df.columns else []
            track_name = st.selectbox("Circuit à importer", track_opts, index=None, placeholder="Choisir un circuit")
            car_names = st.multiselect("Voitures (vide = toutes)", car_opts)
            track_ids = tracks_df.loc[tracks_df["name"] == track_name, "id"].tolist() if (track_name and track_opts) else []
            car_ids = (cars_df.loc[cars_df["name"].isin(car_names), "id"].tolist() or None) if car_opts else None
            resume_key = f"offset:{track_ids[0] if track_ids else ''}:{','.join(map(str, car_ids or []))}"
            resume_from = st.session_state.get(resume_key, 0)
            label = f"Reprendre l'import (à partir du tour {resume_from + 1})" if resume_from else "Importer les tours"
            if st.button(label, type="primary", disabled=not (token and track_ids)):
                with st.status("Import Garage 61…", expanded=True) as status:
                    try:
                        df, nxt, note = g61.G61Client(token, log=st.write).laps(team_slug=team_slug, tracks=track_ids, cars=car_ids,
                                                                                age_days=age, session_types=sess_ids, start_offset=resume_from)
                        n = store.save_laps(df)
                        ui.refresh_laps()
                        if nxt is None:
                            st.session_state.pop(resume_key, None)
                            status.update(label=f"Import terminé : {n} nouveaux tours", state="complete", expanded=False)
                        else:
                            st.session_state[resume_key] = nxt
                            status.update(label=f"{n} tours enregistrés — {note}", state="running", expanded=False)
                            st.warning("Reclique dans 2 minutes pour récupérer la suite.")
                    except Exception as e:  # noqa: BLE001
                        status.update(label="Import impossible", state="error")
                        st.error(str(e))

    if is_admin and not ui.is_demo():
        with st.expander("Télémétrie iRacing (.ibt) — secours"):
            st.caption("Fichiers dans Documents/iRacing/telemetry sur le PC du pilote.")
            ibt_files = st.file_uploader("Déposer un ou plusieurs .ibt", type=["ibt"], accept_multiple_files=True, label_visibility="collapsed")
            if ibt_files and st.button("Importer la télémétrie", type="primary"):
                import tempfile
                from ibt_import import read_ibt
                total = 0
                for f in ibt_files:
                    try:
                        with tempfile.NamedTemporaryFile(suffix=".ibt", delete=False) as tmp:
                            tmp.write(f.getbuffer())
                            path = tmp.name
                        df = read_ibt(path, source_name=f.name)
                        n = store.save_laps(df)
                        total += n
                        st.write(f"{f.name} : {len(df)} tours, {n} nouveaux")
                    except Exception as e:  # noqa: BLE001
                        st.error(f"{f.name} : {e}")
                ui.refresh_laps()
                st.success(f"{total} nouveaux tours enregistrés")

    st.subheader("Tours bruts")
    if laps.empty:
        st.info("Aucun tour en base.")
    else:
        c1, c2 = st.columns(2)
        cars = ["Toutes"] + sorted(laps["car"].dropna().unique().tolist())
        car = c1.selectbox("Voiture", cars)
        sub = laps if car == "Toutes" else laps[laps["car"] == car]
        tracks = ["Tous"] + sorted(sub["track"].dropna().unique().tolist())
        track = c2.selectbox("Circuit", tracks)
        if track != "Tous":
            sub = sub[sub["track"] == track]
        cols = [c for c in sub.columns if c not in ("raw", "imported_at", "team_code")]
        st.dataframe(sub[cols].sort_values("start_time", ascending=False).head(2000), hide_index=True, use_container_width=True)
        st.caption(f"{len(sub)} tours · {sub['driver'].nunique()} pilotes · {sub['track'].nunique()} circuits")
        if "raw" in sub.columns and not sub.empty:
            with st.expander("Structure brute du 1er tour"):
                try:
                    st.json(json.loads(sub["raw"].iloc[0]))
                except Exception:  # noqa: BLE001
                    st.code(str(sub["raw"].iloc[0]))

    if is_admin and not ui.is_demo():
        with st.expander("Zone sensible"):
            confirm = st.checkbox("Je confirme vouloir effacer tous les tours de l'équipe")
            if st.button("Vider la base", disabled=not confirm):
                store.clear_laps()
                ui.refresh_laps()
                st.success("Base vidée")
                st.rerun()


# =====================================================================================
# Navigation
# =====================================================================================
import os as _os
_test_page = _os.environ.get("RBM_TEST_PAGE")  # tests automatisés : force une page sans navigation
if _test_page:
    {"iracing": page_iracing, "league": page_league, "special": page_special, "pilotes": page_pilots, "live": page_live, "donnees": page_data}[_test_page]()
    st.stop()

pg = st.navigation({
    "Courses": [
        st.Page(page_iracing, title="Week-end iRacing", icon="🏁", default=True, url_path="iracing"),
        st.Page(page_league, title="Course league", icon="🏆", url_path="league"),
        st.Page(page_special, title="Événements spéciaux", icon="⭐", url_path="special"),
    ],
    "Équipe": [
        st.Page(page_pilots, title="Pilotes", icon="👥", url_path="pilotes"),
        st.Page(page_live, title="En piste", icon="📡", url_path="live"),
        st.Page(page_data, title="Données", icon="🗄️", url_path="donnees"),
    ],
})
pg.run()
