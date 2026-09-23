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
import json
import logging
import requests
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

ICLOUD_API_BASE = "https://p{partition}-sharedstreams.icloud.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; FrameUpdater/1.0)",
    "Content-Type": "application/json",
}


def _extract_token(share_url: str) -> str | None:
    """Extrait le token de partage depuis l'URL iCloud."""
    # Format: https://www.icloud.com/photos/XXXXXXXX
    match = re.search(r'/photos/([A-Za-z0-9_-]+)', share_url)
    return match.group(1) if match else None


def _get_partition(token: str) -> int:
    """Détermine la partition iCloud (1-9) basée sur le token."""
    # Les partitions iCloud vont de 01 à 09
    # La partition est déterminée côté Apple, on essaie depuis 01
    return 1


def fetch_album_metadata(share_url: str) -> dict | None:
    """
    Récupère les métadonnées de l'album partagé iCloud.
    Retourne un dict avec les infos de l'album ou None si erreur.
    """
    token = _extract_token(share_url)
    if not token:
        logger.error(f"URL iCloud invalide: {share_url}")
        return None

    # Essayer les partitions 1-9
    for partition in range(1, 10):
        api_url = f"{ICLOUD_API_BASE.format(partition=str(partition).zfill(2))}/{token}/sharedstreams/webstream"
        try:
            resp = requests.post(
                api_url,
                headers=HEADERS,
                json={"streamCtag": None},
                timeout=10,
            )
            if resp.status_code == 200:
                logger.info(f"Album iCloud trouvé sur partition {partition}")
                return {"token": token, "partition": partition, **resp.json()}
            elif resp.status_code == 330:
                # Redirection vers une autre partition
                data = resp.json()
                redirect_partition = data.get("X-Apple-MMe-Redir-Partition", partition)
                logger.info(f"Redirection vers partition {redirect_partition}")
                partition = int(redirect_partition)
                continue
        except requests.RequestException as e:
            logger.debug(f"Partition {partition} inaccessible: {e}")
            continue

    logger.error(f"Impossible d'accéder à l'album iCloud: {share_url}")
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
    partition = meta["partition"]
    photos_raw = meta.get("photos", [])

    if not photos_raw:
        logger.warning("Album vide ou format inattendu")
        return []

    # Récupérer les URLs de téléchargement
    guids = [p["photoGuid"] for p in photos_raw]
    api_url = f"{ICLOUD_API_BASE.format(partition=str(partition).zfill(2))}/{token}/sharedstreams/webasseturls"

    try:
        resp = requests.post(
            api_url,
            headers=HEADERS,
            json={"photoGuids": guids},
            timeout=15,
        )
        resp.raise_for_status()
        asset_data = resp.json()
    except requests.RequestException as e:
        logger.error(f"Erreur récupération URLs iCloud: {e}")
        return []

    photos = []
    items = asset_data.get("items", {})

    for photo in photos_raw:
        guid = photo.get("photoGuid")
        derivatives = photo.get("derivatives", {})

        # Prendre la meilleure résolution disponible
        best = None
        best_size = 0
        for key, deriv in derivatives.items():
            w = int(deriv.get("width", 0))
            h = int(deriv.get("height", 0))
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
    Retourne le chemin local ou None en cas d'échec.
    """
    os.makedirs(cache_dir, exist_ok=True)

    filename = photo.get("filename", f"{photo['guid']}.jpg")
    local_path = os.path.join(cache_dir, filename)

    if os.path.exists(local_path):
        logger.debug(f"Déjà en cache: {local_path}")
        return local_path

    url = photo.get("url", "")
    if not url:
        logger.warning(f"Pas d'URL pour la photo {photo.get('guid')}")
        return None

    try:
        logger.info(f"Téléchargement iCloud: {filename}")
        resp = requests.get(url, timeout=30, stream=True)
        resp.raise_for_status()

        with open(local_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)

        return local_path

    except requests.RequestException as e:
        logger.error(f"Erreur téléchargement iCloud {filename}: {e}")
        return None
