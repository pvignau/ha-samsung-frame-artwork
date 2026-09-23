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

# Partitions iCloud connues (p01 à p09) et garde-fou contre les redirections circulaires.
_PARTITIONS = tuple(range(1, 10))
_MAX_REDIRECTS = 10


def _extract_token(share_url: str) -> str | None:
    """Extrait le token de partage depuis l'URL iCloud."""
    # Format: https://www.icloud.com/photos/XXXXXXXX
    match = re.search(r'/photos/([A-Za-z0-9_-]+)', share_url)
    return match.group(1) if match else None


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


def fetch_album_metadata(share_url: str) -> dict | None:
    """
    Récupère les métadonnées de l'album partagé iCloud.
    Retourne un dict avec les infos de l'album ou None si erreur.

    Apple répond parfois par un HTTP 330 indiquant la bonne partition : cette
    redirection est suivie en priorité, avec un nombre de sauts borné et un suivi
    des partitions déjà essayées pour éviter toute boucle infinie.
    """
    token = _extract_token(share_url)
    if not token:
        logger.error(f"URL iCloud invalide: {share_url}")
        return None

    tried: set[int] = set()
    redirects = 0
    # File d'attente : la partition indiquée par une redirection passe devant.
    queue: list[int] = list(_PARTITIONS)

    while queue:
        partition = queue.pop(0)
        if partition in tried:
            continue
        tried.add(partition)

        api_url = (
            f"{ICLOUD_API_BASE.format(partition=str(partition).zfill(2))}"
            f"/{token}/sharedstreams/webstream"
        )
        try:
            resp = requests.post(
                api_url,
                headers=HEADERS,
                json={"streamCtag": None},
                timeout=10,
            )
        except requests.RequestException as e:
            logger.debug(f"Partition {partition} inaccessible: {e}")
            continue

        if resp.status_code == 200:
            logger.info(f"Album iCloud trouvé sur partition {partition}")
            try:
                payload = resp.json()
            except ValueError as e:
                logger.warning(f"Réponse iCloud illisible sur la partition {partition}: {e}")
                continue
            return {"token": token, "partition": partition, **payload}

        if resp.status_code == 330:
            # Redirection vers une autre partition
            try:
                data = resp.json()
            except ValueError:
                data = {}
            raw = data.get("X-Apple-MMe-Redir-Partition") or resp.headers.get(
                "X-Apple-MMe-Redir-Partition"
            )
            try:
                target = int(str(raw).lstrip("p") or partition)
            except (TypeError, ValueError):
                logger.warning(f"Partition de redirection illisible: {raw!r}")
                continue

            redirects += 1
            if redirects > _MAX_REDIRECTS:
                logger.error("Trop de redirections iCloud, abandon")
                break
            if target in tried:
                logger.debug(f"Redirection vers la partition {target} déjà essayée, ignorée")
                continue

            logger.info(f"Redirection vers partition {target}")
            # La partition indiquée par Apple est essayée en priorité.
            queue.insert(0, target)
            continue

        if resp.status_code == 404:
            logger.debug(f"Partition {partition}: album inconnu (404)")
            continue

        logger.warning(
            f"Statut HTTP inattendu {resp.status_code} sur la partition {partition} "
            f"pour l'album iCloud"
        )

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
    api_url = (
        f"{ICLOUD_API_BASE.format(partition=str(partition).zfill(2))}"
        f"/{token}/sharedstreams/webasseturls"
    )

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
