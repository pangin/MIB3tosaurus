import re
import base64
import math
import json
import argparse
import threading
import webbrowser
from pathlib import Path
from typing import Callable, Generator, Iterable, List, Optional, Tuple, Union
from datetime import datetime
import mimetypes

import pandas as pd
import folium


def prompt_user_for_folder() -> Path:
    """Prompt l'utilisateur pour le dossier des logs et retourne un Path valide (CLI)."""
    while True:
        user_input = input("Entrez le chemin du dossier contenant les fichiers logs: ").strip().strip('"')
        if not user_input:
            print("Chemin vide. Veuillez réessayer.")
            continue
        p = Path(user_input)
        if p.is_dir():
            return p
        print("Chemin invalide ou non trouvé. Veuillez réessayer.")


def iter_log_files(root_dir: Path) -> Generator[Path, None, None]:
    """Itère récursivement tous les fichiers du dossier racine."""
    for file_path in root_dir.rglob('*'):
        if file_path.is_file():
            yield file_path


def extract_printable_strings_from_bin(
    file_path: Path,
    min_len: int = 4,
    chunk_size: int = 4 * 1024 * 1024,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> str:
    """Extrait des chaînes imprimables (style utilitaire 'strings') en mode flux.

    - Lit le fichier par blocs pour éviter de charger 500MB en mémoire d'un coup
    - Met à jour la progression via progress_cb(bytes_read, total_bytes)
    """
    try:
        total = file_path.stat().st_size
        f = file_path.open('rb')
    except Exception:
        return ""
    parts: List[str] = []
    current: bytearray = bytearray()

    def flush_current():
        if len(current) >= min_len:
            try:
                parts.append(current.decode('ascii', errors='ignore'))
            except Exception:
                try:
                    parts.append(current.decode('utf-8', errors='ignore'))
                except Exception:
                    parts.append(current.decode('latin-1', errors='ignore'))
        current.clear()

    PRINTABLE = set(range(0x20, 0x7F))  # visibles
    PRINTABLE.add(0x09)  # tab
    PRINTABLE.add(0x0A)  # LF
    PRINTABLE.add(0x0D)  # CR

    read_total = 0
    try:
        with f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                read_total += len(chunk)
                for b in chunk:
                    if b in PRINTABLE:
                        current.append(b)
                    else:
                        flush_current()
                if progress_cb is not None:
                    try:
                        progress_cb(read_total, total)
                    except Exception:
                        pass
            # fin de fichier
            flush_current()
    except Exception:
        return ""
    return "\n".join(parts)


def extract_printable_strings(
    file_path: Path,
    min_len: int = 4,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> str:
    """Wrapper convivial: extrait les chaînes imprimables d'un fichier (binaire ou non).

    Utilise la version streaming pour permettre la progression.
    """
    return extract_printable_strings_from_bin(file_path, min_len=min_len, progress_cb=progress_cb)


def read_file_text(file_path: Path) -> str:
    """Lit un fichier en texte; si binaire, extrait les chaînes imprimables.

    Combine intelligemment lecture texte et extraction binaire pour maximiser la couverture.
    """
    text_candidates: List[str] = []
    # Tentatives lecture texte directe
    for enc in ('utf-8', 'latin-1'):
        try:
            t = file_path.read_text(encoding=enc, errors='ignore')
            if t and t.strip():
                text_candidates.append(t)
                break
        except Exception:
            continue
    # Extraction de chaînes depuis binaire
    bin_strings = extract_printable_strings_from_bin(file_path, min_len=4)
    if bin_strings:
        text_candidates.append(bin_strings)
    # Agrégation
    if not text_candidates:
        return ""
    if len(text_candidates) == 1:
        return text_candidates[0]
    return "\n".join(text_candidates)


def find_matching_brace(text: str, start_index: int) -> Optional[int]:
    """Trouve l'index de l'accolade fermante correspondante en gérant les chaînes et échappements.

    Retourne l'index de '}' correspondant au '{' à start_index, sinon None si non trouvé.
    """
    depth = 0
    in_string = False
    string_char = ''
    i = start_index
    while i < len(text):
        c = text[i]
        if in_string:
            if c == '\\':
                i += 2
                continue
            if c == string_char:
                in_string = False
            i += 1
            continue
        if c == '"' or c == "'":
            in_string = True
            string_char = c
            i += 1
            continue
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def generate_candidate_starts(
    text: str,
    keywords: Optional[List[str]] = None,
    window_radius: int = 10000,
) -> Iterable[int]:
    """Génère des indices candidats probables pour le début d'un objet JSON.

    - Par défaut: scan global des motifs {"<cle>":
    - Avec keywords: d'abord scan limité à des fenêtres autour des mots-clés (accélère),
      puis (optionnel) scan global si keywords est None.
    """
    key_pattern = re.compile(r"\{\s*\"[A-Za-z0-9_\-.]+\"\s*:", re.MULTILINE)

    if keywords:
        # 1) Pour chaque occurrence de mot-clé, remonter pour trouver le début de l'objet enveloppant
        def find_enclosing_object_start(pos: int, backtrack_limit: int = 200000) -> Optional[int]:
            L = max(0, pos - backtrack_limit)
            region = text[L:pos]
            # Trouver toutes les '{' dans la zone et tester de la plus proche à la plus lointaine
            brace_positions = [L + m.start() for m in re.finditer(r'\{', region)]
            for s in reversed(brace_positions):
                end = find_matching_brace(text, s)
                if end is not None and end >= pos:
                    return s
            return None

        seen: set[int] = set()
        kw_regex = re.compile('|'.join([re.escape(k) for k in keywords]), re.IGNORECASE)
        for km in kw_regex.finditer(text):
            start_candidate = find_enclosing_object_start(km.start())
            if start_candidate is not None and start_candidate not in seen:
                seen.add(start_candidate)
                yield start_candidate

        # 2) Complément: dans des fenêtres autour des mots-clés, rechercher des motifs de début d'objet
        windows: List[Tuple[int, int]] = []
        for km in kw_regex.finditer(text):
            a = max(0, km.start() - window_radius)
            b = min(len(text), km.end() + window_radius)
            if windows and a <= windows[-1][1]:
                windows[-1] = (windows[-1][0], max(windows[-1][1], b))
            else:
                windows.append((a, b))
        for a, b in windows:
            segment = text[a:b]
            for m in key_pattern.finditer(segment):
                s = a + m.start()
                if s not in seen:
                    seen.add(s)
                    yield s
        return

    # Scan global
    for m in key_pattern.finditer(text):
        yield m.start()


def extract_json_blocks(raw_text: str, keywords: Optional[List[str]] = None) -> List[str]:
    """Extrait des blocs JSON équilibrés à partir d'un texte brut (optimisé).

    Si keywords est fourni, on limite la recherche aux fenêtres autour des mots-clés.
    """
    blocks: List[str] = []
    last_end = -1
    for start in generate_candidate_starts(raw_text, keywords=keywords):
        if start <= last_end:
            continue
        end = find_matching_brace(raw_text, start)
        if end is None:
            continue
        block = raw_text[start:end + 1]
        blocks.append(block)
        last_end = end
    return blocks


def extract_position_records_from_text(
    raw_text: str,
    source_file: Path,
    root_dir: Path,
    window: int = 600,
) -> pd.DataFrame:
    """Extrait toutes les occurrences de positions (lat;lon) même hors JSON et retourne un DataFrame.

    - Motif principal: position": "<lat>;<lon>;" (tolérant aux espaces, quotes, séparateurs ; , ou espace)
    - Cherche dans une fenêtre autour de chaque occurrence des champs contextuels: localTime, name
    - Ajoute Fichier_Source
    - Ajoute latitude/longitude directement
    """
    pattern = re.compile(
        r'position"?\s*[:=]\s*[\"\']?\s*'
        r'(?P<lat>[+-]?\d+(?:\.\d+)?)\s*[;,\s]\s*'
        r'(?P<lon>[+-]?\d+(?:\.\d+)?)\s*;?',
        re.IGNORECASE,
    )
    # Contexte
    re_localtime = re.compile(r'localTime\s*:\s*"(?P<lt>[^"]+)"', re.IGNORECASE)
    re_usedon = re.compile(r'usedOn\s*:\s*"(?P<u>[^"]+)"', re.IGNORECASE)
    re_createdon = re.compile(r'createdOn\s*:\s*"(?P<c>[^"]+)"', re.IGNORECASE)
    re_iso = re.compile(r'(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))')
    re_name = re.compile(r'name\s*:\s*"(?P<name>[^"]+)"', re.IGNORECASE)

    records: List[dict] = []
    text_len = len(raw_text)
    for m in pattern.finditer(raw_text):
        try:
            lat = float(m.group('lat'))
            lon = float(m.group('lon'))
        except Exception:
            continue
        a = max(0, m.start() - window)
        b = min(text_len, m.end() + window)
        ctx = raw_text[a:b]
        local_time = None
        name = None
        lt_m = re_localtime.search(ctx)
        if lt_m:
            local_time = lt_m.group('lt')
        if local_time is None:
            um = re_usedon.search(ctx)
            if um:
                local_time = um.group('u')
        if local_time is None:
            cm = re_createdon.search(ctx)
            if cm:
                local_time = cm.group('c')
        if local_time is None:
            tm = re_iso.search(ctx)
            if tm:
                local_time = tm.group('ts')
        nm = re_name.search(ctx)
        if nm:
            name = nm.group('name')
        try:
            rel = str(source_file.relative_to(root_dir))
        except Exception:
            rel = str(source_file)
        records.append({
            'location.position': f"{lat};{lon};",
            'position': f"{lat:.6f} ,{lon:.6f};",
            'latitude': lat,
            'longitude': lon,
            'usedOn': local_time,
            'localTime': local_time,
            'timestamp': local_time,
            'name': name,
            'Fichier_Source': rel,
            'Extraction_Mode': 'regex_position',
        })

    if not records:
        return pd.DataFrame()
    return pd.DataFrame.from_records(records)


def remove_trailing_commas(s: str) -> str:
    return re.sub(r',\s*([}\]])', r'\1', s)


def replace_py_literals(s: str) -> str:
    s = re.sub(r'(?<![\w$])True(?![\w$])', 'true', s)
    s = re.sub(r'(?<![\w$])False(?![\w$])', 'false', s)
    s = re.sub(r'(?<![\w$])None(?![\w$])', 'null', s)
    return s


def replace_single_quotes_keys_and_values(s: str) -> str:
    # Clés en quotes simples -> doubles
    s = re.sub(r"([\{,\[]\s*)'(.*?)'(\s*:)", r'\1"\2"\3', s)
    # Valeurs chaînes en quotes simples -> doubles
    s = re.sub(r"(:\s*)'(.*?)'(\s*[},\]])", r'\1"\2"\3', s)
    return s


def try_parse_json(block: str) -> Optional[dict]:
    """Tente de parser un bloc en JSON en appliquant des corrections tolérantes si nécessaire."""
    transforms = [
        lambda x: x,
        remove_trailing_commas,
        replace_py_literals,
        replace_single_quotes_keys_and_values,
        lambda x: replace_single_quotes_keys_and_values(remove_trailing_commas(x)),
        lambda x: replace_py_literals(remove_trailing_commas(x)),
        lambda x: replace_py_literals(replace_single_quotes_keys_and_values(remove_trailing_commas(x))),
    ]
    for transform in transforms:
        candidate = transform(block)
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue
    return None


def normalize_records(objs: List[dict], source_file: Path, root_dir: Path) -> pd.DataFrame:
    """Aplati une liste d'objets JSON en DataFrame (colonnes dot-notées)."""
    rows: List[pd.DataFrame] = []
    for obj in objs:
        try:
            flat = pd.json_normalize(obj, sep='.')
        except Exception:
            # Fallback: mettre l'objet brut en texte si normalize échoue
            flat = pd.DataFrame({"raw_object": [json.dumps(obj, ensure_ascii=False)]})
        try:
            rel = str(source_file.relative_to(root_dir))
        except Exception:
            rel = str(source_file)
        flat["Fichier_Source"] = rel
        rows.append(flat)
    if not rows:
        return pd.DataFrame()
    # Exclure les DataFrames vides ou entièrement NA pour éviter le FutureWarning de concat
    filtered_rows: List[pd.DataFrame] = []
    for df in rows:
        if df.empty:
            continue
        try:
            if df.isna().all().all():
                continue
        except Exception:
            pass
        filtered_rows.append(df)
    if not filtered_rows:
        return pd.DataFrame()
    return pd.concat(filtered_rows, ignore_index=True)


def parse_lat_lon_from_row(row: pd.Series) -> Tuple[Optional[float], Optional[float]]:
    def to_float(val: Union[str, int, float, None]) -> Optional[float]:
        if val is None:
            return None
        try:
            return float(val)
        except Exception:
            return None

    # 1) location.position.latitude / longitude
    lat = None
    lon = None
    for lat_key, lon_key in [
        ("location.position.latitude", "location.position.longitude"),
        ("location.latitude", "location.longitude"),
        ("latitude", "longitude"),
        ("location.position.lat", "location.position.lon"),
        ("location.lat", "location.lon"),
    ]:
        if lat_key in row and lon_key in row:
            lat = to_float(row.get(lat_key))
            lon = to_float(row.get(lon_key))
            if lat is not None and lon is not None:
                return lat, lon

    # 2) location.position as string "lat,lon" or "lon,lat"
    if "location.position" in row and isinstance(row["location.position"], str):
        txt = row["location.position"].strip()
        nums = re.findall(r'[-+]?\d+(?:\.\d+)?', txt)
        if len(nums) >= 2:
            a = to_float(nums[0])
            b = to_float(nums[1])
            if a is not None and b is not None:
                # Heuristique: latitude in [-90, 90]
                if -90.0 <= a <= 90.0 and -180.0 <= b <= 180.0:
                    return a, b
                if -90.0 <= b <= 90.0 and -180.0 <= a <= 180.0:
                    return b, a

    # 3) location.position.coordinates as list [lon, lat] or [lat, lon]
    if "location.position.coordinates" in row:
        coords = row["location.position.coordinates"]
        if isinstance(coords, (list, tuple)) and len(coords) >= 2:
            a = to_float(coords[0])
            b = to_float(coords[1])
            if a is not None and b is not None:
                if -90.0 <= a <= 90.0 and -180.0 <= b <= 180.0:
                    return a, b
                if -90.0 <= b <= 90.0 and -180.0 <= a <= 180.0:
                    return b, a

    return None, None


def build_folium_map(df: pd.DataFrame) -> folium.Map:
    if df.empty:
        return folium.Map(location=[0, 0], zoom_start=2, control_scale=True)

    def valid_coord(x: Optional[float], is_lat: bool) -> bool:
        if x is None:
            return False
        try:
            if isinstance(x, float) and math.isnan(x):
                return False
        except Exception:
            pass
        if is_lat:
            return -90.0 <= float(x) <= 90.0
        return -180.0 <= float(x) <= 180.0

    points: List[Tuple[float, float, int]] = []  # (lat, lon, idx)
    for idx, row in df.iterrows():
        lat, lon = parse_lat_lon_from_row(row)
        if valid_coord(lat, True) and valid_coord(lon, False):
            points.append((float(lat), float(lon), idx))

    if points:
        center_lat = sum(p[0] for p in points) / len(points)
        center_lon = sum(p[1] for p in points) / len(points)
        center = [center_lat, center_lon]
        zoom = 6
    else:
        center = [0, 0]
        zoom = 2

    m = folium.Map(location=center, zoom_start=zoom, control_scale=True)

    for lat, lon, idx in points:
        row = df.iloc[idx]
        used_on = row.get('usedOn') or row.get('localTime') or row.get('createdOn') or 'N/A'
        street_name = row.get('location.street.name') or row.get('location.streetName') or 'N/A'
        city_name = row.get('location.city.name') or row.get('location.cityName') or 'N/A'
        src = row.get('Fichier_Source', 'N/A')

        popup_html = f"<b>usedOn/createdOn:</b> {used_on}<br>" \
                     f"<b>Rue:</b> {street_name}<br>" \
                     f"<b>Ville:</b> {city_name}<br>" \
                     f"<b>Source:</b> {src}"
        try:
            folium.Marker(
                location=[lat, lon],
                popup=folium.Popup(popup_html, max_width=320)
            ).add_to(m)
        except Exception:
            # Ignore les points invalides
            continue

    return m


def dataframe_to_html_table(df: pd.DataFrame) -> str:
    # Remplacer NaN/NaT/None par vide pour un rendu propre
    df_display = df.copy()
    try:
        df_display = df_display.fillna("")
    except Exception:
        pass
    # Convertir proprement les listes/dicts en JSON lisible
    obj_cols = df_display.select_dtypes(include=['object']).columns
    if len(obj_cols) > 0:
        def to_readable(v):
            if isinstance(v, (list, dict)):
                try:
                    return json.dumps(v, ensure_ascii=False)
                except Exception:
                    return str(v)
            return v
        try:
            df_display[obj_cols] = df_display[obj_cols].applymap(to_readable)
        except Exception:
            pass
    table_html = df_display.to_html(index=False, border=0, escape=False)
    container = (
        '<div style="max-width:100%; overflow-x:auto; border:1px solid #ddd; padding:8px;">\n'
        f'{table_html}\n'
        '</div>'
    )
    return container


def find_coredump_directories(root_dir: Path) -> List[Path]:
    """Retourne la liste des dossiers nommés 'Coredump' sous le répertoire racine."""
    dirs: List[Path] = []
    try:
        for p in root_dir.rglob('*'):
            try:
                if p.is_dir() and p.name.lower() == 'coredump':
                    dirs.append(p)
            except Exception:
                continue
    except Exception:
        pass
    return dirs


def _guess_mime_type(path: Path) -> str:
    mt, _ = mimetypes.guess_type(str(path))
    if not mt:
        # Fallback simple sur l'extension
        ext = path.suffix.lower().lstrip('.')
        if ext in ('jpg', 'jpeg'):
            return 'image/jpeg'
        if ext in ('png',):
            return 'image/png'
        if ext in ('gif',):
            return 'image/gif'
        if ext in ('bmp',):
            return 'image/bmp'
        if ext in ('webp',):
            return 'image/webp'
        if ext in ('tif', 'tiff'):
            return 'image/tiff'
        return 'application/octet-stream'
    return mt


def collect_images_from_coredump(root_dir: Path) -> pd.DataFrame:
    """Parcourt les dossiers 'Coredump' et collecte les images avec vignettes et métadonnées.

    Colonnes retournées:
    - Image: balise <img> base64 (vignette, hauteur max 140px)
    - Chemin: chemin relatif depuis root_dir
    - Créé le: horodatage ISO de création (st_ctime)
    - Modifié le: horodatage ISO de modification (st_mtime)
    """
    image_exts = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp', '.tif', '.tiff'}
    records: List[dict] = []
    coredump_dirs = find_coredump_directories(root_dir)
    seen_paths: set[str] = set()
    for cd in coredump_dirs:
        try:
            for f in cd.rglob('*'):
                try:
                    if not f.is_file():
                        continue
                    if f.suffix.lower() not in image_exts:
                        continue
                    f_key = str(f.resolve())
                    if f_key in seen_paths:
                        continue
                    seen_paths.add(f_key)
                    st = f.stat()
                    try:
                        rel = str(f.relative_to(root_dir))
                    except Exception:
                        rel = str(f)
                    # Correction d'inversion: "Créé le" affichera mtime, "Modifié le" affichera ctime
                    created = datetime.fromtimestamp(st.st_mtime).isoformat(sep=' ', timespec='seconds')
                    modified = datetime.fromtimestamp(st.st_ctime).isoformat(sep=' ', timespec='seconds')
                    # Construire la vignette base64 (on intègre l'image brute; style limite la taille à l'affichage)
                    try:
                        b = f.read_bytes()
                        mime = _guess_mime_type(f)
                        data_uri = 'data:' + mime + ';base64,' + base64.b64encode(b).decode('ascii')
                        img_html = f'<img src="{data_uri}" alt="{f.name}" style="max-height:140px; max-width:240px; object-fit:contain; border:1px solid #ddd; padding:2px; background:#fff;" />'
                    except Exception:
                        img_html = ''
                    records.append({
                        'Image': img_html,
                        'Chemin': rel,
                        'Créé le': created,
                        'Modifié le': modified,
                    })
                except Exception:
                    continue
        except Exception:
            continue
    if not records:
        return pd.DataFrame()
    df = pd.DataFrame.from_records(records)
    try:
        # Trier par date de création décroissante, puis modification
        df['__c__'] = pd.to_datetime(df['Créé le'], errors='coerce')
        df['__m__'] = pd.to_datetime(df['Modifié le'], errors='coerce')
        df = df.sort_values(['__c__', '__m__'], ascending=[False, False]).drop(columns=['__c__', '__m__'])
    except Exception:
        pass
    return df


def build_df_for_dedup(original_df: pd.DataFrame) -> pd.DataFrame:
    """Crée une vue du DataFrame où les valeurs non hachables (list, dict, set, ndarray)
    sont converties en chaînes JSON pour permettre drop_duplicates sans erreur.

    On n'altère pas le DataFrame d'origine; on retourne une copie transformée.
    """
    df = original_df.copy()
    obj_cols = df.select_dtypes(include=['object']).columns

    def to_hashable_string(value):
        # conversion ciblée des types non-hachables usuels
        if isinstance(value, (list, dict, set)):
            try:
                return json.dumps(value, ensure_ascii=False, sort_keys=True)
            except Exception:
                return str(value)
        # numpy arrays
        try:
            import numpy as np  # type: ignore
            if isinstance(value, np.ndarray):
                try:
                    return json.dumps(value.tolist(), ensure_ascii=False, sort_keys=True)
                except Exception:
                    return str(value)
        except Exception:
            pass
        return value

    if len(obj_cols) > 0:
        df[obj_cols] = df[obj_cols].applymap(to_hashable_string)
    return df


def _stable_stringify(value) -> str:
    """Convertit n'importe quelle valeur en représentation stable pour signature de ligne.

    - list/dict/set/ndarray -> JSON trié
    - Timestamps/Datetime -> ISO
    - NaN/NaT/None -> 'null'
    - Autres -> str(value)
    """
    try:
        import numpy as np  # type: ignore
        import pandas as pd  # type: ignore  # for NaT
    except Exception:
        np = None  # type: ignore
        pd = None  # type: ignore

    # Null-like
    if value is None:
        return 'null'
    try:
        if np is not None and isinstance(value, float) and (value != value):  # NaN
            return 'null'
    except Exception:
        pass
    if str(type(value)).endswith("'NaTType'>"):
        return 'null'

    # Datetime-like
    try:
        if hasattr(value, 'isoformat'):
            return value.isoformat()
    except Exception:
        pass

    # Containers
    try:
        import numpy as _np  # type: ignore
        if isinstance(value, _np.ndarray):
            value = value.tolist()
    except Exception:
        pass

    if isinstance(value, (list, dict, set, tuple)):
        try:
            # Remplacer set/tuple par liste pour JSON stable
            if isinstance(value, set):
                value = sorted(list(value))
            elif isinstance(value, tuple):
                value = list(value)
            return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        except Exception:
            return str(value)

    # Par défaut
    try:
        return str(value)
    except Exception:
        return repr(value)


def compute_row_signatures(df: pd.DataFrame) -> pd.Series:
    """Construit une signature stable par ligne pour déduplication robuste et rapide."""
    # Ordonner les colonnes pour obtenir une signature indépendante de l'ordre
    cols = list(df.columns)
    cols.sort()
    def row_sig(row: pd.Series) -> str:
        parts = []
        for c in cols:
            parts.append(f"{c}=" + _stable_stringify(row.get(c)))
        return '|'.join(parts)
    return df.apply(row_sig, axis=1)


def build_full_html(map_obj: folium.Map, table_html: str, title: str = "MIB3tosaurus - Parser", logo_data_uri: Optional[str] = None, images_section_html: Optional[str] = None) -> str:
    # Injecte un script dans la carte (iframe) pour permettre le focus sur un marqueur par clé lat,lon
    base_map_html = map_obj.get_root().render()
    injected_script = """
<script>
    (function(){
        function findMapInstance(){
            for (var k in window){
                try { if (window[k] && window[k] instanceof L.Map) return window[k]; } catch(e){}
            }
            return null;
        }
        function collectMarkers(map){
            var markers = [];
            if (!map || !map._layers) return markers;
            for (var id in map._layers){
                var layer = map._layers[id];
                if (layer && typeof layer.getLatLng === 'function' && layer instanceof L.Marker){
                    markers.push(layer);
                }
            }
            return markers;
        }
        function buildRegistry(map){
            var reg = {};
            var list = collectMarkers(map);
            list.forEach(function(m){
                try {
                    var ll = m.getLatLng();
                    var keyLL = ll.lat.toFixed(6) + ',' + ll.lng.toFixed(6);
                    reg[keyLL] = m;
                    // Construire aussi une clé composite avec usedOn/createdOn et Source pour désambiguïser
                    var usedOn = null, src = null;
                    try {
                        if (m.getPopup && m.getPopup()){
                            var html = '' + m.getPopup().getContent();
                            var uIdx = html.indexOf('usedOn/createdOn:');
                            if (uIdx >= 0){
                                var seg = html.slice(uIdx + 'usedOn/createdOn:'.length);
                                var br = seg.indexOf('<');
                                usedOn = (br >= 0 ? seg.slice(0, br) : seg).replace(/<[^>]*>/g,'').trim();
                            }
                            var sIdx = html.indexOf('Source:');
                            if (sIdx >= 0){
                                var seg2 = html.slice(sIdx + 'Source:'.length);
                                var br2 = seg2.indexOf('<');
                                src = (br2 >= 0 ? seg2.slice(0, br2) : seg2).replace(/<[^>]*>/g,'').trim();
                            }
                        }
                    } catch(e){}
                    if (usedOn && src){
                        var composite = keyLL + '|' + usedOn + '|' + src;
                        reg[composite] = m;
                    }
                } catch(e){}
            });
            return reg;
        }
        var __map = null;
        var __reg = null;
        var __focusCircle = null;
        function ensure(){
            if (!__map){ __map = findMapInstance(); }
            if (!__reg && __map){ __reg = buildRegistry(__map); }
            return !!(__map && __reg);
        }
        window.focusMarkerByKey = function(key){
            if (!ensure()) return false;
            var m = __reg[key];
            if (!m) return false;
            var ll = m.getLatLng();
            try { __map.setView(ll, Math.max(__map.getZoom()||2, 14)); } catch(e){}
            try { if (m.getPopup) m.openPopup(); } catch(e){}
            try {
                if (!__focusCircle){
                    __focusCircle = L.circleMarker(ll, {radius: 10, color: 'red', weight: 3, fillOpacity: 0.2});
                    __focusCircle.addTo(__map);
                } else {
                    __focusCircle.setLatLng(ll).bringToFront();
                }
            } catch(e){}
            return true;
        };
        // Rebuild registry if layers change (basic hook)
        setTimeout(function(){ if (ensure()) { __reg = buildRegistry(__map); } }, 500);
    })();
</script>
"""
    map_html = base_map_html.replace('</body>', injected_script + '\n</body>') if '</body>' in base_map_html else base_map_html + injected_script

    def escape_attr_single_quotes(s: str) -> str:
        return s.replace("'", "&#39;")

    iframe_html = f"<iframe id=\"map-iframe\" title=\"Carte Folium\" srcdoc='" + escape_attr_single_quotes(map_html) + "' " \
                  "style=\"width:100%; height:600px; border:1px solid #ddd;\"></iframe>"

    export_js = """
    <script>
    function getColIndexByHeader(table, headerName){
        const ths = table.querySelectorAll('thead th');
        for (let i=0;i<ths.length;i++){ if ((ths[i].innerText||'').trim() === headerName) return i; }
        return -1;
    }
    function tableToCSV(tableId) {
        const table = document.getElementById(tableId);
        const rows = table.querySelectorAll('tr');
        let csv = [];
        for (let i = 0; i < rows.length; i++) {
            const row = [], cols = rows[i].querySelectorAll('th, td');
            for (let j = 0; j < cols.length; j++) {
                let text = cols[j].innerText.replace(/\r?\n|\r/g, ' ').trim();
                if (text.includes('"')) {
                    text = text.replace(/"/g, '""');
                }
                if (text.includes(',') || text.includes('"')) {
                    text = '"' + text + '"';
                }
                row.push(text);
            }
            csv.push(row.join(','));
        }
        return csv.join('\n');
    }
    // Suppression des interactions "voir sur la carte"
    function downloadCSV(tableId, filename) {
        const csv = tableToCSV(tableId);
        const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
        const link = document.createElement('a');
        if (navigator.msSaveBlob) {
            navigator.msSaveBlob(blob, filename);
        } else {
            const url = URL.createObjectURL(blob);
            link.setAttribute('href', url);
            link.setAttribute('download', filename);
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);
            URL.revokeObjectURL(url);
        }
    }
    </script>
    """

    full_html = f"""
<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{title}</title>
    <style>
        body {{ font-family: Arial, Helvetica, sans-serif; margin: 16px; }}
        h1 {{ font-size: 22px; margin-bottom: 12px; }}
        .section {{ margin-bottom: 24px; }}
        .btn {{ display: inline-block; background: #0b5ed7; color: #fff; border: none; padding: 8px 12px; cursor: pointer; border-radius: 4px; text-decoration: none; }}
        .btn:hover {{ background: #0a53be; }}
        table.dataframe {{ border-collapse: collapse; font-size: 12px; }}
        table.dataframe th, table.dataframe td {{ border: 1px solid #ddd; padding: 4px 6px; }}
        table.dataframe th {{ position: sticky; top: 0; background: #f7f7f7; z-index: 1; }}
    </style>
</head>
<body>
    <div style="display:flex; align-items:center; gap:12px;">
        {('<img src="' + logo_data_uri + '" alt="Logo" style="height:40px;">') if logo_data_uri else ''}
        <h1 style="margin:0;">{title}</h1>
    </div>
    <div class="section">
        <h2>Carte interactive</h2>
        {iframe_html}
    </div>
    <div class="section">
        <h2>Données exhaustives</h2>
        <div style="margin-bottom:8px;">
            <button class="btn" onclick="downloadCSV('table-exhaustive', 'export_forensique.csv')">Exporter en CSV</button>
        </div>
        {table_html.replace('<table', '<table id=\"table-exhaustive\"')}
    </div>
    {('<div class=\"section\"><h2>Images extraites (Coredump)</h2>' + images_section_html + '</div>') if images_section_html else ''}
    {export_js}
</body>
</html>
"""
    return full_html


def run_analysis(
    root_dir: Path,
    log: Callable[[str], None] = print,
    open_after: bool = False,
    progress: Optional[Callable[[Path, int, int], None]] = None,
    stage_cb: Optional[Callable[[Path, str], None]] = None,
    parse_progress: Optional[Callable[[Path, int, int], None]] = None,
    files: Optional[List[Path]] = None,
) -> Optional[Path]:
    log("Démarrage de l'analyse...")
    if not root_dir.is_dir():
        log("Le dossier spécifié est introuvable ou invalide.")
        return None

    all_dfs: List[pd.DataFrame] = []

    # Déterminer la liste des fichiers à traiter
    file_iter: Iterable[Path]
    if files:
        file_iter = files
    else:
        file_iter = iter_log_files(root_dir)

    for file_path in file_iter:
        log(f"Traitement du fichier: {file_path}")
        # Extraction 'strings' avec progression (streaming)
        def _cb(br: int, tot: int, fp: Path = file_path):
            if progress is not None:
                try:
                    progress(fp, br, tot)
                except Exception:
                    pass
        text = extract_printable_strings(file_path, progress_cb=_cb if progress else None)
        if not text:
            continue

        # Annonce de l'étape: recherche des blocs JSON
        if stage_cb is not None:
            try:
                stage_cb(file_path, 'finding_blocks')
            except Exception:
                pass
        # Utilise un mot-clé pertinent pour accélérer la recherche (ex: "position")
        blocks = extract_json_blocks(text, keywords=["position"]) if len(text) > 200000 else extract_json_blocks(text)
        parsed_objs: List[dict] = []
        # Étape parsing avec progression par bloc
        total_blocks = len(blocks)
        if stage_cb is not None:
            try:
                stage_cb(file_path, 'parsing_blocks')
            except Exception:
                pass
        for idx, blk in enumerate(blocks, start=1):
            obj = try_parse_json(blk)
            if obj is not None:
                parsed_objs.append(obj)
            if parse_progress is not None and total_blocks > 0:
                try:
                    parse_progress(file_path, idx, total_blocks)
                except Exception:
                    pass

        # Extraction directe des positions même hors JSON
        pos_df = extract_position_records_from_text(text, file_path, root_dir)
        if len(pos_df) > 0:
            log(f"  -> Positions extraites (regex): {len(pos_df)}")

        if not parsed_objs and pos_df.empty:
            # Rien à agréger pour ce fichier
            continue
        # Étape normalisation
        if stage_cb is not None:
            try:
                stage_cb(file_path, 'normalizing')
            except Exception:
                pass
        # Terminer la progression du fichier courant à 100%
        if progress is not None:
            try:
                tot = file_path.stat().st_size
                progress(file_path, tot, tot)
            except Exception:
                pass

        file_dfs: List[pd.DataFrame] = []
        if parsed_objs:
            df_json = normalize_records(parsed_objs, file_path, root_dir)
            if not df_json.empty:
                # Propager latitude/longitude si absentes à partir de location.position
                if 'latitude' not in df_json.columns or 'longitude' not in df_json.columns:
                    lats: List[Optional[float]] = []
                    lons: List[Optional[float]] = []
                    for _, r in df_json.iterrows():
                        lt, ln = parse_lat_lon_from_row(r)
                        lats.append(lt)
                        lons.append(ln)
                    if 'latitude' not in df_json.columns:
                        df_json['latitude'] = lats
                    if 'longitude' not in df_json.columns:
                        df_json['longitude'] = lons
                # Ajouter/standardiser colonne position "lat ,lon;"
                def _fmt_pos(row: pd.Series) -> Optional[str]:
                    lt = row.get('latitude')
                    ln = row.get('longitude')
                    try:
                        if lt is None or ln is None:
                            return None
                        return f"{float(lt):.6f} ,{float(ln):.6f};"
                    except Exception:
                        return None
                df_json['position'] = df_json.apply(_fmt_pos, axis=1)
                file_dfs.append(df_json)
        if not pos_df.empty:
            file_dfs.append(pos_df)
        if file_dfs:
            all_dfs.append(pd.concat(file_dfs, ignore_index=True))

        # Étape fichier terminé
        if stage_cb is not None:
            try:
                stage_cb(file_path, 'file_done')
            except Exception:
                pass

    if not all_dfs:
        log("Aucune donnée exploitable (JSON/positions) n'a été extraite. Rapport non généré.")
        return None

    log("Agrégation des données...")
    full_df = pd.concat(all_dfs, ignore_index=True) if len(all_dfs) > 1 else all_dfs[0]

    # Dédupliquer (lignes strictement identiques)
    before = len(full_df)
    # Déduplication robuste: d'abord par positions uniques si disponibles, sinon par signature stable
    if 'latitude' in full_df.columns and 'longitude' in full_df.columns:
        # Arrondir pour tolérer de très légères variations numériques
        lat_round = full_df['latitude'].round(6)
        lon_round = full_df['longitude'].round(6)
        mask = ~(lat_round.astype(str) + '|' + lon_round.astype(str)).duplicated()
        full_df = full_df.loc[mask].reset_index(drop=True)
    else:
        sig = compute_row_signatures(full_df)
        mask = ~sig.duplicated()
        full_df = full_df.loc[mask].reset_index(drop=True)
    after = len(full_df)
    log(f"Doublons supprimés: {before - after}")

    # Unifier le timestamp si disponible (localTime/createdOn -> usedOn si manquant)
    if 'usedOn' not in full_df.columns:
        if 'localTime' in full_df.columns and full_df['localTime'].notna().any():
            full_df['usedOn'] = full_df['localTime']
        elif 'timestamp' in full_df.columns and full_df['timestamp'].notna().any():
            full_df['usedOn'] = full_df['timestamp']
        elif 'createdOn' in full_df.columns and full_df['createdOn'].notna().any():
            full_df['usedOn'] = full_df['createdOn']

    # Trier par usedOn puis createdOn (descendant)
    sort_key = None
    if 'usedOn' in full_df.columns:
        sort_key = 'usedOn'
    elif 'createdOn' in full_df.columns:
        sort_key = 'createdOn'

    if sort_key is not None:
        full_df['__sort_dt__'] = pd.to_datetime(full_df[sort_key], errors='coerce', utc=True)
        full_df = full_df.sort_values('__sort_dt__', ascending=False, na_position='last').drop(columns=['__sort_dt__'])
    else:
        log("Colonnes usedOn/createdOn introuvables pour le tri. Tri ignoré.")

    # Colonnes latitude / longitude et colonne position formatée
    log("Extraction des coordonnées géographiques...")
    if 'latitude' not in full_df.columns or 'longitude' not in full_df.columns:
        lats: List[Optional[float]] = []
        lons: List[Optional[float]] = []
        for _, r in full_df.iterrows():
            lat, lon = parse_lat_lon_from_row(r)
            lats.append(lat)
            lons.append(lon)
        full_df['latitude'] = lats
        full_df['longitude'] = lons
    # Ajouter/mettre à jour la colonne position homogène "lat ,lon;"
    def fmt_pos(row: pd.Series) -> Optional[str]:
        lat = row.get('latitude')
        lon = row.get('longitude')
        try:
            if lat is None or lon is None:
                return None
            return f"{float(lat):.6f} ,{float(lon):.6f};"
        except Exception:
            return None
    full_df['position'] = full_df.apply(fmt_pos, axis=1)

    # Carte Folium
    log("Génération de la carte interactive...")
    fmap = build_folium_map(full_df)

    # Restreindre aux colonnes demandées, dans cet ordre
    desired_cols = [
        'Fichier_Source', 'latitude', 'longitude', 'position', 'location.position',
        'usedOn', 'localTime', 'timestamp', 'Extraction_Mode'
    ]
    present_cols = [c for c in desired_cols if c in full_df.columns]
    missing_cols = [c for c in desired_cols if c not in full_df.columns]
    for c in missing_cols:
        full_df[c] = ''
    full_df = full_df[desired_cols]

    # Tableau HTML exhaustif (sans colonne "Voir sur la carte")
    log("Rendu du tableau HTML...")
    if 'Action' in full_df.columns:
        try:
            full_df = full_df.drop(columns=['Action'])
        except Exception:
            pass
    table_html = dataframe_to_html_table(full_df)

    # Section images extraites (Coredump)
    log("Recherche des images sous Coredump...")
    images_df = collect_images_from_coredump(root_dir)
    images_section_html: Optional[str] = None
    if not images_df.empty:
        log(f"Images trouvées: {len(images_df)}")
        images_table_html = dataframe_to_html_table(images_df)
        images_section_html = images_table_html.replace('<table', '<table id=\"table-images\"')
    else:
        log("Aucune image trouvée sous Coredump.")

    # Rapport complet (avec logo si présent)
    log("Assemblage du rapport HTML...")
    logo_uri: Optional[str] = None
    try:
        logo_path = Path.cwd() / 'logo.png'
        if logo_path.is_file():
            b = logo_path.read_bytes()
            logo_uri = 'data:image/png;base64,' + base64.b64encode(b).decode('ascii')
    except Exception:
        logo_uri = None
    html = build_full_html(fmap, table_html, logo_data_uri=logo_uri, images_section_html=images_section_html)
    out_file = Path.cwd() / 'rapport_forensique_exhaustif.html'
    out_file.write_text(html, encoding='utf-8')

    log(f"Rapport généré: {out_file}")
    if open_after:
        try:
            webbrowser.open(out_file.as_uri())
        except Exception:
            pass
    return out_file


def launch_gui():
    try:
        import tkinter as tk
        from tkinter import ttk, filedialog, messagebox
    except Exception as e:
        print("Tkinter indisponible: ", e)
        print("Bascule en mode CLI.")
        root_dir = prompt_user_for_folder()
        run_analysis(root_dir, print, open_after=True)
        return

    root = tk.Tk()
    root.title("MIB3tosaurus - Parser")
    root.geometry("900x600")

    # Bandeau avec logo si disponible
    try:
        from tkinter import PhotoImage
        logo_path = Path.cwd() / 'logo.png'
        logo_img = None
        if logo_path.is_file():
            logo_img = PhotoImage(file=str(logo_path))
            try:
                # Réduire le logo si trop grand (hauteur max ~48px)
                h = int(logo_img.height())
                if h > 48:
                    factor = max(1, (h + 47) // 48)
                    logo_img = logo_img.subsample(factor, factor)
            except Exception:
                pass
        if logo_img is not None:
            # Définir aussi l'icône de la fenêtre (barre de titre)
            try:
                iw = int(logo_img.width())
                ih = int(logo_img.height())
                max_dim = max(iw, ih)
                icon_img = logo_img
                if max_dim > 32:
                    f = max(1, (max_dim + 31) // 32)
                    icon_img = logo_img.subsample(f, f)
                root.iconphoto(True, icon_img)
            except Exception:
                pass
            header = ttk.Frame(root, padding=(10, 6))
            header.pack(fill='x')
            lbl_logo = ttk.Label(header, image=logo_img)
            lbl_logo.image = logo_img
            lbl_logo.pack(side='left', padx=(0, 8))
            lbl_title = ttk.Label(header, text="MIB3tosaurus - Parser", font=("Segoe UI", 12, "bold"))
            lbl_title.pack(side='left')
    except Exception:
        pass

    folder_var = tk.StringVar()
    auto_open_var = tk.BooleanVar(value=True)
    progress_var = tk.DoubleVar(value=0.0)
    progress_text_var = tk.StringVar(value="En attente…")

    def log_to_text(msg: str):
        text_logs.configure(state='normal')
        text_logs.insert('end', msg + "\n")
        text_logs.see('end')
        text_logs.configure(state='disabled')

    def choose_folder():
        path = filedialog.askdirectory(title="Sélectionnez le dossier des logs")
        if path:
            folder_var.set(path)

    def choose_files():
        files = filedialog.askopenfilenames(title="Sélectionnez un ou plusieurs fichiers à analyser")
        if files:
            folder_var.set(";".join(files))

    def set_controls_state(state: str):
        btn_browse.configure(state=state)
        btn_run.configure(state=state)
        btn_browse_files.configure(state=state)

    def on_run_clicked():
        raw_sel = folder_var.get().strip()
        selected_files: Optional[List[Path]] = None
        path: Optional[Path] = None
        if not raw_sel:
            messagebox.showwarning("Sélection manquante", "Veuillez sélectionner un dossier ou des fichiers.")
            return
        if ";" in raw_sel:
            # Mode multi-fichiers explicite
            selected_files = [Path(s) for s in raw_sel.split(";") if s]
            if not selected_files:
                messagebox.showerror("Fichiers invalides", "Aucun fichier exploitable n'a été sélectionné.")
                return
            for fp in selected_files:
                if not fp.is_file():
                    messagebox.showerror("Fichier invalide", f"Introuvable: {fp}")
                    return
            # Parent commun si possible
            try:
                base = selected_files[0].parent
                for fp in selected_files[1:]:
                    while base and not str(fp.parent).startswith(str(base)):
                        base = base.parent
                path = base if base else selected_files[0].parent
            except Exception:
                path = selected_files[0].parent
        else:
            path = Path(raw_sel)
            if not path.is_dir():
                messagebox.showerror("Chemin invalide", "Le dossier sélectionné est invalide.")
                return

        text_logs.configure(state='normal')
        text_logs.delete('1.0', 'end')
        text_logs.configure(state='disabled')

        set_controls_state('disabled')

        def worker():
            out: Optional[Path] = None
            error: Optional[Exception] = None
            try:
                def gui_progress(fp: Path, bytes_read: int, total: int):
                    percent = 0.0
                    if total > 0:
                        percent = (bytes_read / total) * 100.0
                    def upd():
                        try:
                            pbar.stop()
                        except Exception:
                            pass
                        pbar.configure(mode='determinate')
                        progress_var.set(percent)
                        progress_text_var.set(f"Lecture: {fp.name} ({percent:.1f}%)")
                    root.after(0, upd)

                def gui_stage(fp: Path, stage: str):
                    def upd():
                        if stage == 'finding_blocks':
                            progress_text_var.set(f"Recherche des blocs JSON: {fp.name}")
                            progress_var.set(0.0)
                            pbar.configure(mode='indeterminate')
                            try:
                                pbar.start(12)
                            except Exception:
                                pass
                        elif stage == 'parsing_blocks':
                            try:
                                pbar.stop()
                            except Exception:
                                pass
                            pbar.configure(mode='determinate')
                            progress_var.set(0.0)
                            progress_text_var.set(f"Parsing des blocs JSON: {fp.name}")
                        elif stage == 'normalizing':
                            progress_text_var.set(f"Normalisation des données: {fp.name}")
                            progress_var.set(0.0)
                            pbar.configure(mode='indeterminate')
                            try:
                                pbar.start(12)
                            except Exception:
                                pass
                        elif stage == 'file_done':
                            try:
                                pbar.stop()
                            except Exception:
                                pass
                            pbar.configure(mode='determinate')
                            progress_var.set(100.0)
                            progress_text_var.set(f"Terminé: {fp.name}")
                    root.after(0, upd)

                def gui_parse_progress(fp: Path, current: int, total: int):
                    percent = 0.0
                    if total > 0:
                        percent = (current / total) * 100.0
                    def upd():
                        try:
                            pbar.stop()
                        except Exception:
                            pass
                        pbar.configure(mode='determinate')
                        progress_var.set(percent)
                        progress_text_var.set(f"Parsing: {fp.name} ({current}/{total}, {percent:.1f}%)")
                    root.after(0, upd)

                out = run_analysis(
                    path,
                    log_to_text,
                    open_after=False,
                    progress=gui_progress,
                    stage_cb=gui_stage,
                    parse_progress=gui_parse_progress,
                    files=selected_files,
                )
            except Exception as ex:
                error = ex
            finally:
                def finalize():
                    set_controls_state('normal')
                    try:
                        pbar.stop()
                    except Exception:
                        pass
                    progress_var.set(0.0)
                    progress_text_var.set("Prêt.")
                    if error:
                        messagebox.showerror("Erreur", f"Une erreur est survenue: {error}")
                    else:
                        if out is None:
                            messagebox.showinfo("Terminé", "Aucun JSON valide n'a été extrait.")
                        else:
                            messagebox.showinfo("Terminé", f"Rapport généré:\n{out}")
                            if auto_open_var.get():
                                try:
                                    webbrowser.open(out.as_uri())
                                except Exception:
                                    pass
                root.after(0, finalize)

        threading.Thread(target=worker, daemon=True).start()

    # Layout
    frm_top = ttk.Frame(root, padding=10)
    frm_top.pack(fill='x')

    lbl = ttk.Label(frm_top, text="Dossier des logs:")
    lbl.pack(side='left')

    entry = ttk.Entry(frm_top, textvariable=folder_var)
    entry.pack(side='left', fill='x', expand=True, padx=8)

    btn_browse = ttk.Button(frm_top, text="Parcourir dossier...", command=choose_folder)
    btn_browse.pack(side='left')
    btn_browse_files = ttk.Button(frm_top, text="Choisir fichiers...", command=choose_files)
    btn_browse_files.pack(side='left', padx=(6, 0))

    frm_opts = ttk.Frame(root, padding=(10, 0))
    frm_opts.pack(fill='x')

    chk_open = ttk.Checkbutton(frm_opts, text="Ouvrir automatiquement le rapport", variable=auto_open_var)
    chk_open.pack(side='left')

    btn_run = ttk.Button(frm_opts, text="Analyser", command=on_run_clicked)
    btn_run.pack(side='right')

    # Barre de progression
    frm_prog = ttk.Frame(root, padding=(10, 0))
    frm_prog.pack(fill='x')
    lbl_prog = ttk.Label(frm_prog, textvariable=progress_text_var)
    lbl_prog.pack(side='left', padx=(0, 10))
    pbar = ttk.Progressbar(frm_prog, variable=progress_var, maximum=100.0, length=300)
    pbar.pack(side='left', fill='x', expand=True)

    frm_logs = ttk.Frame(root, padding=10)
    frm_logs.pack(fill='both', expand=True)

    text_logs = tk.Text(frm_logs, wrap='word', state='disabled')
    scroll = ttk.Scrollbar(frm_logs, orient='vertical', command=text_logs.yview)
    text_logs.configure(yscrollcommand=scroll.set)

    text_logs.pack(side='left', fill='both', expand=True)
    scroll.pack(side='right', fill='y')

    root.mainloop()


def main():
    parser = argparse.ArgumentParser(description="Analyse forensique JSON exhaustive + rapport HTML")
    parser.add_argument('--cli', action='store_true', help="Mode CLI (sinon GUI)")
    parser.add_argument('--root', type=str, help="Dossier racine des logs (CLI)")
    parser.add_argument('--no-open', action='store_true', help="Ne pas ouvrir le rapport après génération (CLI)")
    args = parser.parse_args()

    if args.cli:
        root_dir = Path(args.root) if args.root else prompt_user_for_folder()
        out = run_analysis(root_dir, print, open_after=not args.no_open)
        if out is None:
            print("Aucun rapport généré.")
        return

    launch_gui()


if __name__ == '__main__':
    main()
