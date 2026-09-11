"""Relais Bleu Mercure — planificateur de relais iRacing alimenté par Garage 61."""
import json

import pandas as pd
import plotly.express as px
import streamlit as st

import g61
from stints import RaceParams, driver_stats, plan_stints

st.set_page_config(page_title="Relais Bleu Mercure", page_icon="🏁", layout="wide")

# --- accès équipe ---------------------------------------------------------
pw = st.secrets.get("APP_PASSWORD")
if pw and not st.session_state.get("auth"):
    st.title("Relais Bleu Mercure")
    if st.text_input("Mot de passe équipe", type="password") == pw:
        st.session_state["auth"] = True
        st.rerun()
    st.stop()

st.title("Relais Bleu Mercure")

# --- barre latérale : source de données ----------------------------------
with st.sidebar:
    st.header("Données")
    token = st.secrets.get("G61_TOKEN")
    team_slug = st.secrets.get("G61_TEAM_SLUG")
    demo = st.toggle("Mode démo (sans Garage 61)", value=not token)

    if not demo:
        if not token:
            st.error("Ajoute G61_TOKEN dans .streamlit/secrets.toml")
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

        track_name = st.selectbox("Circuit à importer", tracks_df["name"].tolist() if not tracks_df.empty else [],
                                  index=None, placeholder="Choisir un circuit")
        car_names = st.multiselect("Voitures (vide = toutes)", cars_df["name"].tolist() if not cars_df.empty else [])
        track_ids = tracks_df.loc[tracks_df["name"] == track_name, "id"].tolist() if track_name else []
        car_ids = cars_df.loc[cars_df["name"].isin(car_names), "id"].tolist() or None

        if st.button("Tester la connexion", disabled=not token):
            try:
                r = g61.requests.get(g61.BASE_URL + "me", headers={"Authorization": f"Bearer {token}"}, timeout=15)
                if r.ok:
                    st.success(f"Connecté : {r.json().get('name', r.json())}")
                else:
                    st.error(f"HTTP {r.status_code} — {r.text[:400]}")
            except Exception as e:  # noqa: BLE001
                st.error(f"Erreur réseau : {e}")

        if st.button("Vider la base locale"):
            g61.clear_laps()
            st.success("Base vidée")
            st.rerun()

        resume_key = f"offset:{track_ids[0] if track_ids else ''}:{','.join(map(str, car_ids or []))}"
        resume_from = st.session_state.get(resume_key, 0)
        label = f"Reprendre l'import (à partir du tour {resume_from + 1})" if resume_from else "Importer les tours"
        if st.button(label, type="primary", disabled=not (token and track_ids)):
            with st.status("Import Garage 61…", expanded=True) as status:
                try:
                    df, nxt, note = g61.G61Client(token, log=st.write).laps(
                        team_slug=team_slug, tracks=track_ids, cars=car_ids, age_days=age,
                        session_types=sess_ids, start_offset=resume_from)
                    n = g61.save_laps(df)
                    if nxt is None:
                        st.session_state.pop(resume_key, None)
                        status.update(label=f"Import terminé : {n} nouveaux tours", state="complete", expanded=False)
                    else:
                        st.session_state[resume_key] = nxt
                        status.update(label=f"{n} tours enregistrés — {note}", state="running", expanded=False)
                        st.warning("Reclique dans 2 minutes pour récupérer la suite.")
                    if not df.empty:
                        with st.expander("Aperçu brut du 1er tour (pour vérifier les champs)"):
                            st.json(g61.LAST_RAW[0] if g61.LAST_RAW else {})
                except Exception as e:  # noqa: BLE001
                    status.update(label="Import impossible", state="error")
                    st.error(str(e))

laps = g61.demo_laps() if demo else g61.load_laps()

if laps.empty:
    st.info("Aucun tour en base. Importe depuis Garage 61 ou active le mode démo.")
    st.stop()

# --- filtres ----------------------------------------------------------------
c1, c2, c3 = st.columns(3)
car = c1.selectbox("Voiture", sorted(laps["car"].dropna().unique()))
track = c2.selectbox("Circuit", sorted(laps.loc[laps["car"] == car, "track"].dropna().unique()))
sessions = c3.multiselect("Type de session", sorted(laps["session_type"].unique()), default=sorted(laps["session_type"].unique()))
sel = laps[(laps["car"] == car) & (laps["track"] == track) & laps["session_type"].isin(sessions)]

tab_stats, tab_plan, tab_crews, tab_laps = st.tabs(["Pilotes", "Paramètres course", "Équipages", "Tours bruts"])

# --- onglet pilotes -----------------------------------------------------------
with tab_stats:
    trim = st.slider("Tours lents écartés (%)", 0, 30, 10, help="Écarte les tours les plus lents (trafic, erreurs) du calcul du rythme.") / 100
    stats = driver_stats(sel, trim)
    if stats.empty:
        st.warning("Pas assez de tours propres pour cette sélection.")
    else:
        st.dataframe(
            stats.style.format({"Meilleur": "{:.3f}", "Rythme moyen": "{:.3f}", "Écart-type": "{:.3f}",
                                "Conso / tour (L)": "{:.2f}", "Conso max (L)": "{:.2f}"}),
            hide_index=True, use_container_width=True,
        )
        clean = sel[sel["clean"]].dropna(subset=["lap_time"])
        fig = px.box(clean, x="driver", y="lap_time", color="driver", points="all",
                     labels={"driver": "", "lap_time": "Temps au tour (s)"}, title="Distribution des temps au tour")
        fig.update_layout(showlegend=False, height=420)
        st.plotly_chart(fig, use_container_width=True)

# --- onglet paramètres course --------------------------------------------------
with tab_plan:
    if stats.empty:
        st.warning("Calcule d'abord les statistiques pilotes.")
    else:
        st.caption("Paramètres communs à toutes les voitures engagées. Le plan de chaque voiture est dans l'onglet Équipages.")
        a, b, c, d = st.columns(4)
        duration = a.number_input("Durée de course (min)", 30, 1500, 360, step=30)
        tank = b.number_input("Réservoir (L)", 20.0, 200.0, 100.0, step=1.0)
        pit_loss = c.number_input("Perte par arrêt hors ravitaillement (s)", 10.0, 180.0, 60.0, step=5.0,
                                  help="Entrée + sortie des stands + changement de pilote, sans le temps de remplissage.")
        refuel = d.number_input("Débit ravitaillement (L/s)", 0.5, 10.0, 3.0, step=0.1)
        e, f = st.columns(2)
        margin = e.number_input("Marge carburant par défaut (L)", 0.0, 10.0, 2.0, step=0.5)
        max_stint = f.number_input("Relais max par défaut (min, 0 = aucun)", 0, 300, 0, step=10,
                                   help="Limite règlementaire de temps de volant consécutif, si la course en impose une.")

        usable = tank - margin
        st.markdown(f"Avec ces paramètres, un pilote consommant **{stats['Conso / tour (L)'].mean():.2f} L/tour** "
                    f"(moyenne équipe) tient **{int(usable // stats['Conso / tour (L)'].mean())} tours** par relais, "
                    f"soit environ **{int(usable // stats['Conso / tour (L)'].mean() * stats['Rythme moyen'].mean() / 60)} min**.")

# --- onglet équipages ---------------------------------------------------------
with tab_crews:
    if stats.empty:
        st.warning("Calcule d'abord les statistiques pilotes.")
    else:
        st.caption("Une colonne par voiture engagée. Les pilotes alternent dans l'ordre où tu les sélectionnes ; "
                   "les paramètres communs viennent de l'onglet Paramètres course.")
        n_crews = st.number_input("Nombre de voitures", 1, 6, 3)
        pilots = stats["Pilote"].tolist()
        crews = []
        cols = st.columns(int(n_crews))
        for i, col in enumerate(cols):
            with col:
                name = st.text_input("Voiture", f"Bleu Mercure #{i + 1}", key=f"crew_name_{i}")
                default = pilots[2 * i: 2 * i + 2] if 2 * i < len(pilots) else []
                drivers = st.multiselect("Pilotes (ordre d'alternance)", pilots, default=default, key=f"crew_drv_{i}")
                crew_margin = st.number_input("Marge carburant (L)", 0.0, 10.0, margin, step=0.5, key=f"crew_margin_{i}")
                crew_stint = st.number_input("Relais max (min, 0 = aucun)", 0, 300, int(max_stint), step=10, key=f"crew_stint_{i}")
                crews.append((name, drivers, crew_margin, crew_stint))

        rows, plans = [], {}
        for name, drivers, crew_margin, crew_stint in crews:
            if not drivers:
                continue
            p = RaceParams(duration_min=duration, tank_l=tank, pit_loss_s=pit_loss, refuel_rate_lps=refuel,
                           fuel_margin_l=crew_margin, max_stint_min=crew_stint or None, driver_order=drivers)
            plan = plan_stints(stats, p)
            plans[name] = plan
            avg_pace = stats.set_index("Pilote").loc[drivers, "Rythme moyen"].mean()
            rows.append({
                "Voiture": name,
                "Pilotes": " / ".join(drivers),
                "Rythme moyen (s)": round(avg_pace, 3),
                "Relais": len(plan),
                "Arrêts": len(plan) - 1,
                "Tours estimés": int(plan["Tours cumulés"].iloc[-1]),
                "Carburant (L)": round(plan["Carburant (L)"].sum(), 1),
                "Temps aux stands (s)": int(pd.to_numeric(plan["Arrêt (s)"], errors="coerce").fillna(0).sum()),
            })

        if rows:
            st.subheader("Comparatif")
            st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
            for name, plan in plans.items():
                with st.expander(f"Plan détaillé — {name}"):
                    st.dataframe(plan, hide_index=True, use_container_width=True)
                    st.download_button("Exporter (CSV)", plan.to_csv(index=False).encode(),
                                       file_name=f"relais_{name.replace(' ', '_')}.csv", mime="text/csv", key=f"dl_{name}")

# --- onglet tours bruts -------------------------------------------------------
with tab_laps:
    cols = [c for c in sel.columns if c not in ("raw", "imported_at")]
    st.dataframe(sel[cols].sort_values("start_time", ascending=False), hide_index=True, use_container_width=True)
    if "raw" in sel.columns and not sel.empty:
        with st.expander("Structure brute renvoyée par Garage 61 (1er tour)"):
            try:
                st.json(json.loads(sel["raw"].iloc[0]))
            except Exception:  # noqa: BLE001
                st.code(sel["raw"].iloc[0])
