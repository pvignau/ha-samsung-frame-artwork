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


def fetch_photo_list(share_url: str) -> list[dict]:
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

        content_type = (resp.headers.get("Content-Type") or "").lower()
        if content_type and not content_type.startswith("image/"):
            logger.error(
                f"Contenu non-image ({content_type or 'inconnu'}) pour la photo {filename}"
            )
            return None

        written = 0
        with open(tmp_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    written += len(chunk)

        if written == 0:
            logger.error(f"Téléchargement vide pour la photo {filename}")
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
