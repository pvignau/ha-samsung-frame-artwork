"""
Source : theframetv.com
Scrape et télécharge des artworks gratuits optimisés pour Samsung The Frame.
"""
from __future__ import annotations

import os
import re
import json
import logging
import requests
from bs4 import BeautifulSoup
from pathlib import Path

logger = logging.getLogger(__name__)

BASE_URL = "https://theframetv.com"
ARTS_URL = f"{BASE_URL}/arts/"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; FrameUpdater/1.0)"
}


def _thumb_to_fullres(thumb_url: str) -> str:
    """Convertit une URL thumbnail en URL full-res en supprimant le suffixe de dimensions."""
    return re.sub(r'-\d+x\d+(\.\w+)$', r'\1', thumb_url)


def fetch_artwork_list(max_pages: int = 5) -> list[dict]:
    """
    Scrape theframetv.com/arts/ et retourne la liste des artworks disponibles.
    Retourne une liste de dicts: [{name, thumb_url, full_url, page_url}]
    """
    artworks = []
    page = 1

    while page <= max_pages:
        url = ARTS_URL if page == 1 else f"{ARTS_URL}page/{page}/"
        logger.info(f"Scraping page {page}: {url}")

        try:
            resp = requests.get(url, headers=HEADERS, timeout=10)
            if resp.status_code == 404:
                break
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.warning(f"Erreur scraping page {page}: {e}")
            break

        soup = BeautifulSoup(resp.text, "html.parser")
        items = soup.select("article, .art-item, .post")

        if not items:
            # Fallback: chercher toutes les images avec les URLs caractéristiques
            items = soup.select("a[href*='/arts/download-free']")

        found = 0
        for item in items:
            # Trouver le lien vers la page de l'artwork
            link = item if item.name == "a" else item.select_one("a[href*='/arts/']")
            if not link:
                continue

            page_url = link.get("href", "")
            if not page_url or "download-free" not in page_url:
                continue

            # Trouver l'image thumbnail
            img = item.select_one("img") if item.name != "a" else None
            if img is None:
                thumb_url = ""
                name = page_url.split("/")[-2].replace("-", " ").title()
            else:
                thumb_url = (
                    img.get("data-lazy-src")
                    or img.get("data-src")
                    or img.get("src")
                    or ""
                )
                name = img.get("alt", page_url.split("/")[-2].replace("-", " ").title())

            full_url = _thumb_to_fullres(thumb_url) if thumb_url else ""

            if page_url:
                artworks.append({
                    "name": name,
                    "thumb_url": thumb_url,
                    "full_url": full_url,
                    "page_url": page_url,
                })
                found += 1

        logger.info(f"  → {found} artworks trouvés sur la page {page}")

        if found == 0:
            break

        page += 1

    logger.info(f"Total artworks theframetv.com: {len(artworks)}")
    return artworks


def _get_fullres_from_page(page_url: str) -> str | None:
    """Récupère l'URL full-res depuis la page individuelle — strip le suffixe de taille."""
    try:
        resp = requests.get(page_url, headers=HEADERS, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        for img in soup.find_all("img"):
            src = (
                img.get("data-lazy-src")
                or img.get("data-src")
                or img.get("src")
                or ""
            )
            if "wp-content/uploads" in src and src.endswith((".jpg", ".jpeg", ".png")):
                return re.sub(r'-\d+x\d+(\.\w+)$', r'\1', src)

    except Exception as e:
        logger.warning(f"Impossible de récupérer full-res depuis {page_url}: {e}")

    return None


def load_index(index_file: str) -> list[dict]:
    """Charge l'index local des artworks depuis index_file."""
    if os.path.exists(index_file):
        with open(index_file, "r") as f:
            return json.load(f)
    return []


def save_index(artworks: list[dict], index_file: str) -> None:
    """Sauvegarde l'index local dans index_file."""
    os.makedirs(os.path.dirname(index_file), exist_ok=True)
    with open(index_file, "w") as f:
        json.dump(artworks, f, indent=2, ensure_ascii=False)


def refresh_index(index_file: str, max_pages: int = 5) -> list[dict]:
    """Rafraîchit l'index en scrapant le site et le sauvegarde dans index_file."""
    artworks = fetch_artwork_list(max_pages=max_pages)
    save_index(artworks, index_file)
    return artworks


def download_image(artwork: dict, cache_dir: str) -> str | None:
    """
    Télécharge l'image full-res d'un artwork dans le cache local.
    Retourne le chemin local du fichier, ou None en cas d'échec.
    """
    os.makedirs(cache_dir, exist_ok=True)

    # Nom de fichier basé sur le slug de la page
    slug = artwork["page_url"].rstrip("/").split("/")[-1]
    local_path = os.path.join(cache_dir, f"{slug}.jpg")

    if os.path.exists(local_path):
        logger.debug(f"Déjà en cache: {local_path}")
        return local_path

    # Essayer l'URL full-res directe
    full_url = artwork.get("full_url", "")

    # Si pas d'URL full-res ou que c'est encore un thumbnail, récupérer depuis la page
    if not full_url or "-806x454" in full_url:
        full_url = _get_fullres_from_page(artwork["page_url"]) or full_url

    if not full_url:
        logger.warning(f"Pas d'URL full-res pour: {artwork['name']}")
        return None

    try:
        logger.info(f"Téléchargement: {artwork['name']}")
        resp = requests.get(full_url, headers=HEADERS, timeout=30, stream=True)
        resp.raise_for_status()

        with open(local_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)

        logger.info(f"  → Sauvegardé: {local_path}")
        return local_path

    except requests.RequestException as e:
        logger.error(f"Erreur téléchargement {full_url}: {e}")
        return None
