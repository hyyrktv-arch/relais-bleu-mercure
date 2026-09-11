"""Relais Bleu Mercure — planificateur de relais iRacing alimenté par Garage 61."""
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
        if st.button("Tester la connexion", disabled=not token):
            try:
                r = g61.requests.get(g61.BASE_URL + "me", headers={"Authorization": f"Bearer {token}"}, timeout=15)
                if r.ok:
                    st.success(f"Connecté : {r.json().get('name', r.json())}")
                else:
                    st.error(f"HTTP {r.status_code} — {r.text[:400]}")
                    st.caption(f"Token : {len(token)} caractères, commence par « {token[:4]}… »")
            except Exception as e:  # noqa: BLE001
                st.error(f"Erreur réseau : {e}")

        if st.button("Importer les tours", type="primary", disabled=not token):
            with st.spinner("Import Garage 61…"):
                try:
                    df = g61.G61Client(token).laps(team_slug=team_slug, age_days=age, session_types=sess_ids)
                    n = g61.save_laps(df)
                    st.success(f"{len(df)} tours récupérés, {n} nouveaux enregistrés")
                except Exception as e:  # noqa: BLE001
                    st.error(f"Import impossible : {e}")

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

tab_stats, tab_plan, tab_crews, tab_laps = st.tabs(["Pilotes", "Plan de relais", "Équipages", "Tours bruts"])

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

# --- onglet plan ------------------------------------------------------------
with tab_plan:
    if stats.empty:
        st.warning("Calcule d'abord les statistiques pilotes.")
    else:
        st.subheader("Paramètres de course")
        a, b, c, d = st.columns(4)
        duration = a.number_input("Durée (min)", 30, 1500, 360, step=30)
        tank = b.number_input("Réservoir (L)", 20.0, 200.0, 100.0, step=1.0)
        pit_loss = c.number_input("Perte par arrêt (s)", 10.0, 180.0, 60.0, step=5.0)
        refuel = d.number_input("Débit ravitaillement (L/s)", 0.5, 10.0, 3.0, step=0.1)
        e, f, g = st.columns(3)
        margin = e.number_input("Marge carburant (L)", 0.0, 10.0, 2.0, step=0.5)
        max_stint = f.number_input("Relais max (min, 0 = aucun)", 0, 300, 0, step=10)
        order = g.multiselect("Ordre des pilotes", stats["Pilote"].tolist(), default=stats["Pilote"].tolist())

        params = RaceParams(duration_min=duration, tank_l=tank, pit_loss_s=pit_loss, refuel_rate_lps=refuel,
                            fuel_margin_l=margin, max_stint_min=max_stint or None, driver_order=order)
        plan = plan_stints(stats, params)

        if plan.empty:
            st.warning("Choisis au moins un pilote.")
        else:
            k1, k2, k3, k4 = st.columns(4)
            k1.metric("Relais", len(plan))
            k2.metric("Arrêts", len(plan) - 1)
            k3.metric("Tours estimés", int(plan["Tours cumulés"].iloc[-1]))
            k4.metric("Carburant total (L)", round(plan["Carburant (L)"].sum(), 1))
            st.dataframe(plan, hide_index=True, use_container_width=True)

            per_driver = plan.groupby("Pilote")["Durée (min)"].sum().reset_index()
            fig = px.bar(per_driver, x="Pilote", y="Durée (min)", title="Temps de volant par pilote", color="Pilote")
            fig.update_layout(showlegend=False, height=320)
            st.plotly_chart(fig, use_container_width=True)

            st.download_button("Exporter le plan (CSV)", plan.to_csv(index=False).encode(),
                               file_name=f"relais_{track}_{int(duration)}min.csv", mime="text/csv")

# --- onglet équipages ---------------------------------------------------------
with tab_crews:
    if stats.empty:
        st.warning("Calcule d'abord les statistiques pilotes.")
    else:
        st.caption("Une ligne par voiture engagée. Les paramètres de course communs sont ceux de l'onglet Plan de relais ; "
                   "chaque équipage garde ses propres pilotes et sa propre marge.")
        n_crews = st.number_input("Nombre de voitures", 1, 6, 3)
        pilots = stats["Pilote"].tolist()
        crews = []
        cols = st.columns(int(n_crews))
        for i, col in enumerate(cols):
            with col:
                name = st.text_input("Voiture", f"Bleu Mercure #{i + 1}", key=f"crew_name_{i}")
                default = pilots[2 * i: 2 * i + 2] if 2 * i < len(pilots) else []
                drivers = st.multiselect("Pilotes", pilots, default=default, key=f"crew_drv_{i}")
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
    st.dataframe(sel.sort_values("start_time", ascending=False), hide_index=True, use_container_width=True)
