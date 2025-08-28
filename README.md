## MIB3tosaurus – Parser MIB3 OI

Outil automatisé pour extraire, normaliser et présenter des informations pertinentes depuis des extractions logiques (coredumps, logs) de systèmes MIB3 OI. Les données initiales proviennent de l'outil **VCDS** via l'export **"Infotainment recorder"**.

### Fonctionnalités principales
- **Ingestion récursive**: sélection d'un dossier ou de fichiers, analyse de tout le contenu de manière récursive.
- **Extraction de chaînes “binaries safe”**: lecture en flux des binaires (type `strings`) pour éviter la saturation mémoire.
- **Détection robuste de JSON**: recherche de blocs JSON équilibrés même noyés dans du texte; tolérance aux erreurs (virgules traînantes, quotes simples, littéraux Python True/False/None, etc.).
- **Positions hors JSON**: extraction via expressions régulières de coordonnées GPS présentes en texte libre.
- **Structuration des preuves**: aplatissement JSON → tableau (pandas), déduplication, tri chronologique (usedOn/localTime/timestamp/createdOn).
- **Carte interactive**: génération d'une carte Folium (OpenStreetMap) avec marqueurs et popups contextuels.
- **Rapport HTML autonome**: un seul fichier comprenant la carte et un tableau exhaustif; bouton d’export CSV pour ce tableau.
- **Images Coredump**: scan récursif des dossiers `Coredump`, extraction des images, vignettes intégrées, chemin relatif et dates de création/modification dans un tableau dédié.

### Prérequis
- Python 3.9+ (recommandé)
- Dépendances Python listées dans `requirements.txt`

Installation rapide:
```bash
python -m venv .venv
.venv\Scripts\activate  # Windows
pip install -r requirements.txt
```

### Utilisation
#### Interface graphique (GUI)
```bash
python forensic_report.py
```
- Sélectionnez un dossier (ou plusieurs fichiers) d’extractions.
- Le rapport `rapport_forensique_exhaustif.html` est généré à la racine du projet et peut être ouvert automatiquement.

#### Ligne de commande (CLI)
```bash
python forensic_report.py --cli --root "E:\Chemin\Vers\Les\Données" [--no-open]
```
- `--cli`: force le mode CLI
- `--root`: dossier racine à analyser
- `--no-open`: n’ouvre pas le rapport après génération

### Entrées et organisation types
- Source des données: export `Infotainment recorder` depuis l’outil VCDS.
- Le script accepte un dossier racine contenant des extractions logiques (dossiers de fichiers, archives, logs). Il scanne récursivement tous les fichiers.
- Les images sont collectées spécifiquement sous les dossiers nommés `Coredump` (où qu’ils se trouvent sous le dossier racine).

Exemple (extrait):
```
data/EXP_#00001245_20220818_08034/EXP_#00009271_20250818_04152/Coredump/
  core-av.mainapp.mib3-....
  startup_334/speech-2522-screenshot.png
  ...
```

### Sorties
- `rapport.html` (à la racine du projet):
  - Carte Folium avec marqueurs.
  - Tableau exhaustif (export CSV possible) des données extraites et normalisées.
  - Tableau des images (vignettes, chemin relatif, dates de création/modification).

### Notes 

- La détection/normalisation JSON emploie des heuristiques tolérantes: examinez les résultats lors d’analyses sensibles.

### Limitations connues
- Certaines structures JSON très corrompues peuvent rester inparsables malgré les corrections.
- Les coordonnées GPS ambigües/partielles peuvent ne pas être géolocalisées.
- Les dates issues des contenus et des métadonnées fichiers peuvent différer; privilégiez la corroboration croisée.

### Dépannage rapide
- Carte vide: vérifiez qu’au moins un couple (latitude, longitude) valide est présent après normalisation.
- Rapport non généré: le dossier ne contenait aucune donnée exploitable (ni JSON valide, ni coordonnées en texte).
- Problèmes d’encodage: le script tente plusieurs encodages et l’extraction “strings”; réessayez sur un sous-ensemble de fichiers si nécessaire.




