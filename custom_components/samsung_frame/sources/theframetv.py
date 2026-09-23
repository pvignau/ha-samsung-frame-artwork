"""
Source : theframetv.com
Scrape et télécharge des artworks gratuits optimisés pour Samsung The Frame.
"""
from __future__ import annotations

import os
import re
import time
import json
import logging
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BASE_URL = "https://theframetv.com"
ARTS_URL = f"{BASE_URL}/arts/"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; FrameUpdater/1.0)"
}

# Le catalogue tient sur une vingtaine de pages de 15 oeuvres. La boucle de
# scraping s'arrete d'elle-meme des qu'une page ne renvoie plus rien, donc cette
# borne n'est qu'un garde-fou : elle doit rester largement au-dessus du nombre
# reel de pages, sans quoi on ne recupere que la tete de liste. Avec une borne
# trop basse (5 auparavant) l'index se limitait aux 75 oeuvres les plus
# recentes, toutes issues du meme lot saisonnier.
DEFAULT_MAX_PAGES = 40

# Sélecteurs des cartes d'artwork sur la grille /arts/ (thème WordPress Raven/Elementor).
# Vérifiés sur le HTML réel : <div class="raven-grid-item raven-post-item ...">
#   <div class="raven-post"><div class="raven-post-image-wrap">
#     <a class="raven-post-image" href="..."><img src="...-806x454.jpg" alt="..."></a>
# Les sélecteurs suivants sont essayés dans l'ordre, le premier qui remonte
# quelque chose gagne. Les suivants sont des filets de sécurité si le thème change.
ITEM_SELECTORS = (
    "div.raven-grid-item, div.raven-post-item",
    "div.raven-post",
    "article, .art-item, .post",
    "a.raven-post-image",
    "a[href*='/arts/download-free']",
)

# Images d'habillage du site à ne jamais confondre avec un artwork.
_IGNORED_IMAGE_PATTERNS = ("the-frame-logo", "unsplash_logo", "biy-me-coffe", "logo")

_SIZE_SUFFIX_RE = re.compile(r'-\d+x\d+(\.\w+)$')


def _thumb_to_fullres(thumb_url: str) -> str:
    """Convertit une URL thumbnail en URL full-res en supprimant le suffixe de dimensions."""
    return _SIZE_SUFFIX_RE.sub(r'\1', thumb_url)


def _best_from_srcset(srcset: str) -> str:
    """
    Retourne l'URL de plus grande résolution déclarée dans un attribut ``srcset``.
    Retourne une chaîne vide si le srcset est vide ou illisible.
    """
    best_url = ""
    best_width = -1

    for candidate in srcset.split(","):
        parts = candidate.strip().split()
        if not parts:
            continue
        url = parts[0].strip()
        if not url:
            continue

        width = 0
        if len(parts) > 1:
            descriptor = parts[1].strip().lower()
            try:
                if descriptor.endswith("w"):
                    width = int(float(descriptor[:-1]))
                elif descriptor.endswith("x"):
                    # densité : on la convertit en pseudo-largeur pour comparer
                    width = int(float(descriptor[:-1]) * 1000)
            except ValueError:
                width = 0

        if width > best_width:
            best_width = width
            best_url = url

    return best_url


def _img_url(img) -> str:
    """
    Extrait l'URL la plus grande disponible d'une balise <img>.
    Gère le lazy-loading WordPress (``data-lazy-src`` / ``data-src``) et les ``srcset``.
    """
    if img is None:
        return ""

    for attr in ("data-lazy-srcset", "srcset", "data-srcset"):
        srcset = img.get(attr) or ""
        if srcset:
            url = _best_from_srcset(srcset)
            if url:
                return url.strip()

    for attr in ("data-lazy-src", "data-src", "data-original", "src"):
        url = (img.get(attr) or "").strip()
        if url and not url.startswith("data:"):
            return url

    return ""


def _is_artwork_url(url: str) -> bool:
    """Vrai si l'URL ressemble à une image d'artwork et non à un logo du site."""
    if not url or "wp-content/uploads" not in url:
        return False
    lowered = url.lower()
    if not lowered.split("?")[0].endswith((".jpg", ".jpeg", ".png", ".webp")):
        return False
    return not any(pattern in lowered for pattern in _IGNORED_IMAGE_PATTERNS)


def _absolute(url: str) -> str:
    """Rend une URL absolue par rapport à theframetv.com."""
    if not url:
        return ""
    if url.startswith("//"):
        return f"https:{url}"
    if url.startswith("/"):
        return f"{BASE_URL}{url}"
    return url


def _name_from_page_url(page_url: str) -> str:
    """Dérive un nom lisible depuis le slug de la page de l'artwork."""
    slug = page_url.rstrip("/").split("/")[-1]
    slug = re.sub(r'^download-free-samsung-(4k-)?frame-tv-arts?-', '', slug)
    return slug.replace("-", " ").strip().title() or "Artwork"


def _parse_items(soup: BeautifulSoup) -> list[dict]:
    """Extrait les artworks d'une page de grille, en essayant les sélecteurs connus."""
    items = []
    for selector in ITEM_SELECTORS:
        items = soup.select(selector)
        if items:
            logger.debug(f"Sélecteur retenu: {selector} ({len(items)} éléments)")
            break

    artworks: list[dict] = []
    seen: set[str] = set()

    for item in items:
        if item.name == "a":
            link = item
        else:
            link = (
                item.select_one("a.raven-post-image[href]")
                or item.select_one("a[href*='/arts/download-free']")
                or item.select_one("a[href*='/arts/']")
            )
        if link is None:
            continue

        page_url = _absolute((link.get("href") or "").strip())
        if not page_url or "download-free" not in page_url:
            continue
        if page_url in seen:
            continue

        img = link.select_one("img") or (item.select_one("img") if item.name != "a" else None)
        thumb_url = _absolute(_img_url(img))
        if thumb_url and not _is_artwork_url(thumb_url):
            thumb_url = ""

        name = ""
        if img is not None:
            name = (img.get("alt") or "").strip()
        if not name:
            name = _name_from_page_url(page_url)

        full_url = _thumb_to_fullres(thumb_url) if thumb_url else ""

        seen.add(page_url)
        artworks.append({
            "name": name,
            "thumb_url": thumb_url,
            "full_url": full_url,
            "page_url": page_url,
        })

    return artworks


def fetch_artwork_list(max_pages: int = DEFAULT_MAX_PAGES) -> list[dict]:
    """
    Scrape theframetv.com/arts/ et retourne la liste des artworks disponibles.
    Retourne une liste de dicts: [{name, thumb_url, full_url, page_url}]
    """
    artworks: list[dict] = []
    known_pages: set[str] = set()
    page = 1

    while page <= max_pages:
        url = ARTS_URL if page == 1 else f"{ARTS_URL}page/{page}/"
        logger.info(f"Scraping page {page}: {url}")

        try:
            resp = requests.get(url, headers=HEADERS, timeout=10)
            if resp.status_code == 404:
                logger.debug(f"Page {page} inexistante (404), fin de la pagination")
                break
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.warning(f"Erreur scraping page {page}: {e}")
            break

        soup = BeautifulSoup(resp.text, "html.parser")
        found = 0
        without_image = 0

        for artwork in _parse_items(soup):
            if artwork["page_url"] in known_pages:
                continue
            known_pages.add(artwork["page_url"])
            artworks.append(artwork)
            found += 1
            if not artwork["full_url"]:
                without_image += 1

        logger.info(f"  → {found} artworks trouvés sur la page {page}")
        if without_image:
            logger.warning(
                f"  → {without_image} artworks sans URL d'image sur la page {page} "
                "(le fallback par page individuelle sera utilisé)"
            )

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

        # L'image principale de l'artwork porte la classe wp-post-image
        # (widget JetEngine "jet-listing-dynamic-image").
        candidates = soup.select(
            "img.wp-post-image, img.jet-listing-dynamic-image__img"
        ) or soup.find_all("img")

        for img in candidates:
            src = _absolute(_img_url(img))
            if _is_artwork_url(src):
                return _thumb_to_fullres(src)

        # Dernier recours : l'image mise en avant déclarée dans les métadonnées Open Graph.
        og = soup.select_one("meta[property='og:image']")
        if og:
            src = _absolute((og.get("content") or "").strip())
            if _is_artwork_url(src):
                return _thumb_to_fullres(src)

    except Exception as e:
        logger.warning(f"Impossible de récupérer full-res depuis {page_url}: {e}")

    return None


def load_index(index_file: str) -> list[dict]:
    """
    Charge l'index local des artworks depuis index_file.
    Retourne une liste vide si le fichier est absent, illisible ou corrompu.
    """
    if not os.path.exists(index_file):
        return []

    try:
        with open(index_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        logger.error(f"Index illisible ou corrompu ({index_file}): {e}")
        return []

    if not isinstance(data, list):
        logger.error(f"Index au format inattendu ({index_file}): liste attendue")
        return []

    return data


def save_index(artworks: list[dict], index_file: str) -> None:
    """
    Sauvegarde l'index local dans index_file de façon atomique
    (écriture dans un fichier temporaire puis os.replace).
    """
    directory = os.path.dirname(index_file)
    if directory:
        os.makedirs(directory, exist_ok=True)

    tmp_file = f"{index_file}.tmp"
    try:
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(artworks, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_file, index_file)
    except OSError as e:
        logger.error(f"Erreur d'écriture de l'index {index_file}: {e}")
        try:
            if os.path.exists(tmp_file):
                os.remove(tmp_file)
        except OSError:
            pass
        raise


def index_age_days(index_file: str) -> float | None:
    """
    Retourne l'âge de l'index en jours (basé sur la date de modification du fichier),
    ou None si le fichier n'existe pas / est inaccessible.
    """
    try:
        mtime = os.path.getmtime(index_file)
    except OSError:
        return None

    return max(0.0, (time.time() - mtime) / 86400.0)


def refresh_index(index_file: str, max_pages: int = DEFAULT_MAX_PAGES) -> list[dict]:
    """
    Rafraîchit l'index en scrapant le site et le sauvegarde dans index_file.

    Protection : si le scraping ne retourne aucun artwork alors que l'index existant
    en contient, l'ancien index est conservé (un scraping raté ne doit pas vider
    le catalogue).
    """
    artworks = fetch_artwork_list(max_pages=max_pages)
    existing = load_index(index_file)

    if not artworks and existing:
        logger.error(
            f"Scraping theframetv.com sans résultat : conservation de l'index "
            f"existant ({len(existing)} artworks)"
        )
        return existing

    save_index(artworks, index_file)
    return artworks


def download_image(artwork: dict, cache_dir: str) -> str | None:
    """
    Télécharge l'image full-res d'un artwork dans le cache local.
    Le téléchargement se fait dans un fichier .part renommé à la fin,
    afin de ne jamais laisser un JPEG tronqué dans le cache.
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
    if not full_url or re.search(r'-\d+x\d+\.\w+$', full_url):
        full_url = _get_fullres_from_page(artwork["page_url"]) or full_url

    if not full_url:
        logger.warning(f"Pas d'URL full-res pour: {artwork['name']}")
        return None

    tmp_path = f"{local_path}.part"

    try:
        logger.info(f"Téléchargement: {artwork['name']}")
        resp = requests.get(full_url, headers=HEADERS, timeout=30, stream=True)
        resp.raise_for_status()

        content_type = (resp.headers.get("Content-Type") or "").lower()
        if content_type and not content_type.startswith("image/"):
            logger.error(
                f"Contenu non-image ({content_type or 'inconnu'}) pour {full_url}"
            )
            return None

        written = 0
        with open(tmp_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    written += len(chunk)

        if written == 0:
            logger.error(f"Téléchargement vide pour {full_url}")
            return None

        os.replace(tmp_path, local_path)
        logger.info(f"  → Sauvegardé: {local_path} ({written} octets)")
        return local_path

    except requests.RequestException as e:
        logger.error(f"Erreur téléchargement {full_url}: {e}")
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
