"""
Source : Album partagé iCloud
Télécharge les photos depuis un album partagé iCloud (lien public, sans authentification).

Pour créer un album partagé :
1. Ouvrir l'app Photos sur iPhone/Mac
2. Album → Nouveau album partagé
3. Activer "Site Web public"
4. Copier l'URL (format: https://www.icloud.com/photos/XXXXXXXX)
"""

from __future__ import annotations

import os
import re
import base64
import json
import logging
import requests

logger = logging.getLogger(__name__)

ICLOUD_API_BASE = "https://p{partition}-sharedstreams.icloud.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; FrameUpdater/1.0)",
    "Content-Type": "application/json",
}

# La partition n'est pas a deviner : elle est encodee dans le token lui-meme,
# sur ses caracteres d'index 1 et 2, en base62. Exemples verifies :
#   B12GfnH8tC0ZuK -> "12" -> 1*62 + 2  = 64   -> p64-sharedstreams.icloud.com
#   D2Av3xm1...    -> "2A" -> 2*62 + 10 = 134  -> p134-sharedstreams.icloud.com
# Les partitions montent donc bien au-dela de p09.
_BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_DEFAULT_PARTITION = 1

# Si la partition derivee est mauvaise, Apple repond 330 avec le bon hote dans
# X-Apple-MMe-Host. Garde-fou contre une chaine de redirections circulaire.
_MAX_REDIRECTS = 5


# Trois formes de lien public selon l'epoque :
#   https://www.icloud.com/photos/#B0abcdef          (la plus ancienne)
#   https://www.icloud.com/sharedalbum/#B0abcdef
#   https://photos.icloud.com/shared/album/B0abcdef  (actuelle)
# Le '#' est optionnel, et l'app Photos ajoute parfois le nom de l'album en
# second fragment : .../#B0abcdef#Vacances. Le token s'arrete donc au premier
# '#', '/' ou '?' rencontre. Doit rester coherent avec ICLOUD_URL_RE du
# config_flow, qui valide la saisie de l'utilisateur.
_TOKEN_RE = re.compile(
    r"/(?:photos|sharedalbum|shared/album)/#?([A-Za-z0-9_-]+)",
    re.IGNORECASE,
)


def extract_token(share_url: str) -> str | None:
    """Extrait le token de partage depuis l'URL iCloud, ou None si illisible."""
    match = _TOKEN_RE.search((share_url or "").strip())
    return match.group(1) if match else None


# Ancien nom, conserve pour compatibilite interne.
_extract_token = extract_token


def _safe_filename(filename: str, fallback: str) -> str:
    """
    Assainit un nom de fichier provenant de l'API iCloud (source externe).
    Empêche toute traversée de chemin ('/', '\\', '..') hors du répertoire de cache.
    """
    # On ne garde que le composant final, quel que soit le séparateur utilisé.
    name = str(filename or "").replace("\\", "/").split("/")[-1]
    name = os.path.basename(name).strip()

    # Neutraliser les caractères problématiques et les noms spéciaux.
    name = re.sub(r'[^A-Za-z0-9._-]', "_", name)
    name = name.lstrip(".")

    if not name or name in (".", ".."):
        name = re.sub(r'[^A-Za-z0-9._-]', "_", str(fallback or "photo")) or "photo"

    # Garde une marge sous la limite classique de 255 octets des systèmes de fichiers.
    if len(name) > 200:
        root, ext = os.path.splitext(name)
        name = root[:200 - len(ext)] + ext

    return name


def _partition_from_token(token: str) -> int:
    """Derive le numero de partition encode dans le token (base62 des car. 1 et 2)."""
    if len(token) < 3:
        return _DEFAULT_PARTITION
    value = 0
    for char in token[1:3]:
        index = _BASE62.find(char)
        if index < 0:
            logger.debug(f"Caractere non base62 dans le token: {char!r}")
            return _DEFAULT_PARTITION
        value = value * 62 + index
    return value or _DEFAULT_PARTITION


def _base_url(partition: int) -> str:
    return ICLOUD_API_BASE.format(partition=partition)


def _host_to_base_url(host: str) -> str | None:
    """Convertit l'hote renvoye par une redirection 330 en URL de base."""
    host = (host or "").strip().strip("/")
    if host.startswith("http://") or host.startswith("https://"):
        host = host.split("//", 1)[1]
    if not re.fullmatch(r"p\d+-sharedstreams\.icloud\.com", host, re.IGNORECASE):
        return None
    return f"https://{host}"


def fetch_album_metadata(share_url: str) -> dict | None:
    """
    Récupère les métadonnées de l'album partagé iCloud.
    Retourne un dict avec les infos de l'album ou None si erreur.

    La partition est dérivée du token ; si elle est erronée, Apple répond par un
    HTTP 330 contenant le bon hôte dans X-Apple-MMe-Host, qui est alors suivi.
    """
    token = extract_token(share_url)
    if not token:
        logger.error(f"URL iCloud invalide: {share_url}")
        return None

    base_url = _base_url(_partition_from_token(token))
    logger.debug(f"Partition dérivée du token: {base_url}")
    tried: set[str] = set()

    for _ in range(_MAX_REDIRECTS):
        if base_url in tried:
            logger.debug(f"Hôte déjà essayé, arrêt: {base_url}")
            break
        tried.add(base_url)

        api_url = f"{base_url}/{token}/sharedstreams/webstream"
        try:
            resp = requests.post(
                api_url, headers=HEADERS, json={"streamCtag": None}, timeout=10
            )
        except requests.RequestException as e:
            logger.warning(f"Album iCloud injoignable sur {base_url}: {e}")
            return None

        if resp.status_code == 200:
            try:
                payload = resp.json()
            except ValueError as e:
                logger.warning(f"Réponse iCloud illisible ({base_url}): {e}")
                return None
            logger.info(f"Album iCloud trouvé sur {base_url}")
            return {"token": token, "base_url": base_url, **payload}

        if resp.status_code == 330:
            try:
                data = resp.json()
            except ValueError:
                data = {}
            host = data.get("X-Apple-MMe-Host") or resp.headers.get("X-Apple-MMe-Host")
            target = _host_to_base_url(host) if host else None
            if not target:
                logger.warning(f"Redirection iCloud illisible: {host!r}")
                return None
            logger.info(f"Redirection iCloud vers {target}")
            base_url = target
            continue

        if resp.status_code == 404:
            logger.error(
                "Album iCloud introuvable (404) : le lien de partage est peut-être "
                "expiré, ou le « Site Web public » n'est pas activé sur l'album."
            )
            return None

        logger.warning(
            f"Statut HTTP inattendu {resp.status_code} depuis {base_url} pour l'album iCloud"
        )
        return None

    logger.error(f"Impossible d'accéder à l'album iCloud après redirections: {share_url}")
    return None


def _fetch_photo_list_sharedstreams(share_url: str) -> list[dict]:
    """
    Retourne la liste des photos de l'album partagé.
    Chaque photo est un dict avec: {guid, filename, url, width, height, created}
    """
    meta = fetch_album_metadata(share_url)
    if not meta:
        return []

    token = meta["token"]
    base_url = meta["base_url"]
    photos_raw = meta.get("photos", [])

    if not photos_raw:
        logger.warning("Album vide ou format inattendu")
        return []

    # Récupérer les URLs de téléchargement
    guids = [p["photoGuid"] for p in photos_raw]
    api_url = f"{base_url}/{token}/sharedstreams/webasseturls"

    try:
        resp = requests.post(
            api_url,
            headers=HEADERS,
            json={"photoGuids": guids},
            timeout=15,
        )
        if resp.status_code != 200:
            logger.warning(
                f"Statut HTTP inattendu {resp.status_code} lors de la récupération "
                "des URLs iCloud"
            )
        resp.raise_for_status()
        asset_data = resp.json()
    except requests.RequestException as e:
        logger.error(f"Erreur récupération URLs iCloud: {e}")
        return []
    except ValueError as e:
        logger.error(f"Réponse iCloud illisible (URLs d'assets): {e}")
        return []

    photos = []
    items = asset_data.get("items", {})

    for photo in photos_raw:
        guid = photo.get("photoGuid")
        derivatives = photo.get("derivatives", {})

        # Prendre la meilleure résolution disponible
        best = None
        best_size = 0
        for deriv in derivatives.values():
            try:
                w = int(deriv.get("width", 0))
                h = int(deriv.get("height", 0))
            except (TypeError, ValueError):
                continue
            if w * h > best_size:
                best_size = w * h
                best = deriv

        if not best:
            continue

        checksum = best.get("checksum", "")
        asset_url_info = items.get(checksum, {})
        url_location = asset_url_info.get("url_location", {})
        download_url = url_location.get("url", "")

        photos.append({
            "guid": guid,
            "filename": photo.get("filename", f"{guid}.jpg"),
            "url": download_url,
            "width": best.get("width", 0),
            "height": best.get("height", 0),
            "created": photo.get("dateCreated", ""),
        })

    logger.info(f"iCloud album: {len(photos)} photos trouvées")
    return photos


# ─────────────────────────────────────────────────────────────────────────────
# Albums partagés modernes (CloudKit)
#
# Les liens récents (photos.icloud.com/shared/album/...) ne sont plus servis par
# l'API sharedstreams : Apple les résout via CloudKit. Séquence relevée sur le
# client web officiel, en accès anonyme :
#   1. POST ckdatabasews.icloud.com/.../public/records/resolve?sharing_url_key=TOKEN
#      corps {"shortGUIDs":[{"value":TOKEN}]}
#      -> results[0].anonymousPublicAccess = {token, tokenTTL, databasePartition}
#         results[0].zoneID                = zone de l'album
#   2. POST <databasePartition>/.../shared/changes/zone?publicAccessAuthToken=...
#      corps {"zones":[{"zoneID":...}]}  (sans syncToken = tout le contenu)
#      -> enregistrements CPLMaster portant les URL de téléchargement signées
#
# API non documentée, obtenue par observation : elle peut changer sans préavis.
# C'est pourquoi l'ancienne implémentation est conservée en repli.
# ─────────────────────────────────────────────────────────────────────────────

_CK_CONTAINER = "com.apple.photos.cloud"
_CK_BASE = f"https://ckdatabasews.icloud.com/database/1/{_CK_CONTAINER}/production"
_CK_BUILD = "2634BuildBeta18"
_CK_CLIENT_ID = "00000000-0000-4000-8000-000000000000"
_CK_HEADERS = {
    "Content-Type": "text/plain;charset=UTF-8",
    "Origin": "https://photos.icloud.com",
    "Referer": "https://photos.icloud.com/",
    "User-Agent": HEADERS["User-Agent"],
}
# Pillow ne lit pas le HEIC sans greffon : on ne prend l'original que s'il est
# déjà dans un format sûr, sinon la dérivée JPEG générée par Apple.
_CK_SAFE_ORIGINAL_TYPES = ("public.jpeg", "public.png")
_CK_MAX_PAGES = 20


def _ck_params(token: str, **extra: str) -> dict:
    return {
        "remapEnums": "true",
        "getCurrentSyncToken": "true",
        "clientBuildNumber": _CK_BUILD,
        "clientMasteringNumber": _CK_BUILD,
        "sharing_url_key": token,
        **extra,
    }


def _ck_field(fields: dict, name: str, default=None):
    """Valeur brute d'un champ CloudKit."""
    entry = fields.get(name)
    return entry.get("value", default) if isinstance(entry, dict) else default


def _ck_decode_filename(fields: dict, fallback: str) -> str:
    """filenameEnc est le nom de fichier encodé en base64."""
    raw = _ck_field(fields, "filenameEnc")
    if isinstance(raw, str) and raw:
        try:
            name = base64.b64decode(raw).decode("utf-8", "replace").strip()
            if name:
                return name
        except (ValueError, TypeError):
            pass
    return fallback


def _ck_resolve(token: str) -> dict | None:
    """Résout le lien de partage : jeton anonyme, partition et zone de l'album."""
    try:
        resp = requests.post(
            f"{_CK_BASE}/public/records/resolve",
            params=_ck_params(token),
            headers=_CK_HEADERS,
            data=json.dumps({"shortGUIDs": [{"value": token}]}),
            timeout=15,
        )
    except requests.RequestException as e:
        logger.debug(f"CloudKit injoignable: {e}")
        return None

    if resp.status_code != 200:
        logger.debug(f"CloudKit resolve a répondu {resp.status_code}")
        return None

    try:
        result = resp.json()["results"][0]
        access = result["anonymousPublicAccess"]
        partition = str(access["databasePartition"]).rstrip("/")
        resolved = {
            "auth_token": access["token"],
            "zone_id": result["zoneID"],
            "base_url": f"{partition}/database/1/{_CK_CONTAINER}/production/shared",
            "title": _ck_field(result.get("share", {}).get("fields", {}), "cloudkit.title"),
        }
    except (ValueError, KeyError, IndexError, TypeError) as e:
        logger.warning(f"Réponse CloudKit inattendue: {e}")
        return None

    if not partition.startswith("https://"):
        logger.warning(f"Partition CloudKit invalide: {partition!r}")
        return None

    logger.info(f"Album iCloud « {resolved['title']} » résolu via CloudKit ({partition})")
    return resolved


def _ck_pick_resource(fields: dict) -> tuple[dict, int, int] | None:
    """Choisit la meilleure ressource lisible par Pillow. (ressource, largeur, hauteur)"""
    original_type = _ck_field(fields, "resOriginalFileType")
    candidates = []
    if original_type in _CK_SAFE_ORIGINAL_TYPES:
        candidates.append(("resOriginalRes", "resOriginalWidth", "resOriginalHeight"))
    candidates += [
        ("resJPEGMedRes", "resJPEGMedWidth", "resJPEGMedHeight"),
        ("resJPEGThumbRes", "resJPEGThumbWidth", "resJPEGThumbHeight"),
    ]
    for res_key, w_key, h_key in candidates:
        res = _ck_field(fields, res_key)
        if isinstance(res, dict) and res.get("downloadURL"):
            return res, _ck_field(fields, w_key, 0), _ck_field(fields, h_key, 0)
    return None


def _ck_list_photos(token: str, resolved: dict) -> list[dict]:
    """Énumère les photos de la zone partagée (pagination via syncToken)."""
    params = _ck_params(
        token,
        publicAccessAuthToken=resolved["auth_token"],
        clientId=_CK_CLIENT_ID,
    )
    photos: list[dict] = []
    sync_token = None

    for _ in range(_CK_MAX_PAGES):
        zone_request: dict = {"zoneID": resolved["zone_id"]}
        if sync_token:
            zone_request["syncToken"] = sync_token
        try:
            resp = requests.post(
                f"{resolved['base_url']}/changes/zone",
                params=params,
                headers=_CK_HEADERS,
                data=json.dumps({"zones": [zone_request]}),
                timeout=30,
            )
            resp.raise_for_status()
            zone = resp.json()["zones"][0]
        except (requests.RequestException, ValueError, KeyError, IndexError) as e:
            logger.warning(f"Listing CloudKit interrompu: {e}")
            break

        for record in zone.get("records", []):
            if record.get("recordType") != "CPLMaster":
                continue
            fields = record.get("fields", {})
            picked = _ck_pick_resource(fields)
            if not picked:
                continue
            resource, width, height = picked
            guid = record.get("recordName", "")
            filename = _ck_decode_filename(fields, f"{guid}.jpg")
            # La dérivée JPEG d'un original HEIC garde un nom en .HEIC : on
            # rétablit l'extension réelle pour ne pas induire le cache en erreur.
            if not filename.lower().endswith((".jpg", ".jpeg")):
                filename = f"{filename.rsplit('.', 1)[0]}.jpg"
            photos.append({
                "guid": guid,
                "filename": filename,
                "url": resource["downloadURL"],
                "width": width,
                "height": height,
                "created": _ck_field(fields, "originalCreationDate", ""),
            })

        sync_token = zone.get("syncToken")
        if not zone.get("moreComing") or not sync_token:
            break

    logger.info(f"Album iCloud (CloudKit) : {len(photos)} photo(s) exploitable(s)")
    return photos


def fetch_photo_list(share_url: str) -> list[dict]:
    """
    Retourne la liste des photos de l'album partagé.
    Chaque photo est un dict avec: {guid, filename, url, width, height, created}

    Essaie d'abord CloudKit (liens récents), puis l'ancienne API sharedstreams.
    """
    token = extract_token(share_url)
    if not token:
        logger.error(f"URL iCloud invalide: {share_url}")
        return []

    resolved = _ck_resolve(token)
    if resolved:
        photos = _ck_list_photos(token, resolved)
        if photos:
            return photos
        logger.warning("Album CloudKit résolu mais aucune photo exploitable")
        return []

    logger.debug("CloudKit n'a pas résolu ce lien, essai de l'API sharedstreams")
    return _fetch_photo_list_sharedstreams(share_url)


def _looks_like_image(head: bytes) -> bool:
    """Reconnaît une image à sa signature, sans se fier au Content-Type annoncé."""
    return (
        head.startswith(b"\xff\xd8\xff")                      # JPEG
        or head.startswith(b"\x89PNG\r\n\x1a\n")              # PNG
        or head.startswith(b"GIF87a") or head.startswith(b"GIF89a")
        or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")     # WebP
        or head[4:12] in (b"ftypheic", b"ftypheix", b"ftyphevc", b"ftypmif1")  # HEIF
    )


def download_image(photo: dict, cache_dir: str) -> str | None:
    """
    Télécharge une photo iCloud dans le cache local.
    Le nom de fichier venant de l'API iCloud est assaini (pas de traversée de chemin),
    et le téléchargement passe par un fichier .part renommé à la fin.
    Retourne le chemin local ou None en cas d'échec.
    """
    os.makedirs(cache_dir, exist_ok=True)

    guid = photo.get("guid") or "photo"
    filename = _safe_filename(photo.get("filename", ""), f"{guid}.jpg")
    local_path = os.path.join(cache_dir, filename)

    if os.path.exists(local_path):
        logger.debug(f"Déjà en cache: {local_path}")
        return local_path

    url = photo.get("url", "")
    if not url:
        logger.warning(f"Pas d'URL pour la photo {photo.get('guid')}")
        return None

    tmp_path = f"{local_path}.part"

    try:
        logger.info(f"Téléchargement iCloud: {filename}")
        resp = requests.get(url, timeout=30, stream=True)
        resp.raise_for_status()

        # Le CDN d'Apple sert les photos en application/octet-stream : se fier au
        # Content-Type rejetterait des images parfaitement valides. On vérifie
        # donc la signature réelle des premiers octets, ce qui est plus fiable.
        written = 0
        head = b""
        with open(tmp_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if not chunk:
                    continue
                if len(head) < 12:
                    head += chunk[: 12 - len(head)]
                f.write(chunk)
                written += len(chunk)

        if written == 0:
            logger.error(f"Téléchargement vide pour la photo {filename}")
            return None

        if not _looks_like_image(head):
            logger.error(
                f"Contenu non reconnu comme une image pour la photo {filename} "
                f"(premiers octets: {head[:4].hex()}, {written} octets)"
            )
            return None

        os.replace(tmp_path, local_path)
        logger.debug(f"  → Sauvegardé: {local_path} ({written} octets)")
        return local_path

    except requests.RequestException as e:
        logger.error(f"Erreur téléchargement iCloud {filename}: {e}")
        return None
    except OSError as e:
        logger.error(f"Erreur d'écriture du cache {local_path}: {e}")
        return None
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
