# Relais Bleu Mercure

Webapp de planification des relais iRacing pour l'équipe, alimentée par les tours enregistrés dans Garage 61.

## Ce que ça fait

- Importe tous les tours de l'équipe depuis l'API Garage 61 (temps, carburant consommé, session, pilote) et les stocke en local (SQLite).
- Calcule par pilote : meilleur tour, rythme moyen (tours lents écartés), régularité, conso moyenne et conso max.
- Génère un plan de relais complet : durée, tours, carburant, arrêts, à partir de la durée de course, du réservoir et de l'ordre des pilotes.
- Export CSV du plan.
- Mode démo intégré pour tester sans token.

## Installation locale

```bash
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # puis renseigner le token
streamlit run app.py
```

### Token Garage 61

1. Se connecter sur garage61.net, aller dans les paramètres du compte, section API / Developer.
2. Créer un *Personal Access Token* et le coller dans `secrets.toml`.
3. Renseigner le slug de l'équipe (visible dans l'URL de la page équipe).

Le token ne voit que tes tours et ceux de tes coéquipiers Garage 61 : il faut que chaque pilote soit membre de l'équipe sur Garage 61 avec l'app de télémétrie active pendant ses séances.

## Déploiement pour l'équipe (gratuit)

1. Pousser le dossier sur un dépôt GitHub **privé**.
2. Sur share.streamlit.io, créer une app pointant sur `app.py`.
3. Dans *Advanced settings > Secrets*, coller le contenu de `secrets.toml`.
4. Partager l'URL et le mot de passe équipe aux pilotes.

La base SQLite est éphémère sur Streamlit Cloud ; configurer Supabase (section ci-dessous) pour la persistance.

## Stockage persistant (Supabase)

Sans configuration, la base est un SQLite éphémère (effacé à chaque redéploiement). Pour conserver les tours :
1. Créer un projet sur supabase.com, coller `supabase_schema.sql` dans SQL Editor et l'exécuter.
2. Project Settings > API : copier l'URL du projet et la clé `service_role`.
3. Ajouter dans les Secrets Streamlit : `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `TEAM_CODE`.

## Import de télémétrie iRacing (.ibt) — sans Garage 61

iRacing enregistre la télémétrie dans `Documents/iRacing/telemetry` (Options > Misc, ou Alt+L en session).
Glisser les fichiers dans la barre latérale : pilote, voiture, circuit, session, temps et carburant sont extraits.

## Structure

```
app.py      interface Streamlit (3 onglets : Pilotes, Plan de relais, Tours bruts)
g61.py      client API Garage 61, normalisation des tours, cache SQLite, données démo
stints.py   statistiques pilotes, moteur de relais (séquences éditables, carburant équilibré, départ imposé)
storage.py  couche de stockage : Supabase si configuré, sinon SQLite
ibt_import.py  lecture des fichiers de télémétrie iRacing (.ibt)
supabase_schema.sql  tables Supabase (équipes, tours, live)
```

## À vérifier au premier import

Les noms de champs dans `normalize_laps` (`lapTime`, `fuelUsed`, `sessionType`, `driver.name`…) suivent la doc publique mais peuvent différer légèrement. Si des colonnes ressortent vides, ouvrir l'onglet *Tours bruts*, comparer avec la doc `garage61.net/developer/endpoints/v1/findLaps` et ajuster la fonction `_pick`.

## Pistes d'évolution

- Dégradation du rythme dans le relais (pneus) au lieu d'un rythme constant.
- Contraintes règlementaires : temps de volant mini/maxi par pilote, pénalité de changement de pilote.
- Comparaison de plusieurs scénarios côte à côte.
- Suivi live pendant la course via la télémétrie iRacing (irsdk).
