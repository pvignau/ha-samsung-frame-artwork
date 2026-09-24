"""
Source: theframetv.com
Scrapes and downloads free artworks optimised for Samsung The Frame.
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

# The catalogue spans about twenty pages of 15 artworks. The scraping loop stops
# by itself as soon as a page returns nothing, so this bound is only a
# safeguard: it must stay well above the real page count, otherwise only the
# head of the list is collected. With a bound that was too low (5 previously)
# the index was limited to the 75 most recent artworks, all from the same
# seasonal batch.
DEFAULT_MAX_PAGES = 40

# Selectors for the artwork cards on the /arts/ grid (Raven/Elementor WordPress
# theme). Verified against the real HTML:
#   <div class="raven-grid-item raven-post-item ...">
#     <div class="raven-post"><div class="raven-post-image-wrap">
#       <a class="raven-post-image" href="..."><img src="...-806x454.jpg" alt="..."></a>
# The selectors below are tried in order and the first one that returns
# something wins. The later ones are safety nets in case the theme changes.
ITEM_SELECTORS = (
    "div.raven-grid-item, div.raven-post-item",
    "div.raven-post",
    "article, .art-item, .post",
    "a.raven-post-image",
    "a[href*='/arts/download-free']",
)

# Site chrome images that must never be mistaken for an artwork.
_IGNORED_IMAGE_PATTERNS = ("the-frame-logo", "unsplash_logo", "biy-me-coffe", "logo")

_SIZE_SUFFIX_RE = re.compile(r'-\d+x\d+(\.\w+)$')


def _thumb_to_fullres(thumb_url: str) -> str:
    """Turn a thumbnail URL into a full-res one by stripping the size suffix."""
    return _SIZE_SUFFIX_RE.sub(r'\1', thumb_url)


def _best_from_srcset(srcset: str) -> str:
    """
    Return the highest-resolution URL declared in a ``srcset`` attribute.
    Returns an empty string when the srcset is empty or unreadable.
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
                    # density: converted to a pseudo-width so it can be compared
                    width = int(float(descriptor[:-1]) * 1000)
            except ValueError:
                width = 0

        if width > best_width:
            best_width = width
            best_url = url

    return best_url


def _img_url(img) -> str:
    """
    Extract the largest URL available from an <img> tag.
    Handles WordPress lazy-loading (``data-lazy-src`` / ``data-src``) and ``srcset``.
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
    """True when the URL looks like an artwork image rather than a site logo."""
    if not url or "wp-content/uploads" not in url:
        return False
    lowered = url.lower()
    if not lowered.split("?")[0].endswith((".jpg", ".jpeg", ".png", ".webp")):
        return False
    return not any(pattern in lowered for pattern in _IGNORED_IMAGE_PATTERNS)


def _absolute(url: str) -> str:
    """Make a URL absolute relative to theframetv.com."""
    if not url:
        return ""
    if url.startswith("//"):
        return f"https:{url}"
    if url.startswith("/"):
        return f"{BASE_URL}{url}"
    return url


def _name_from_page_url(page_url: str) -> str:
    """Derive a readable name from the slug of the artwork page."""
    slug = page_url.rstrip("/").split("/")[-1]
    slug = re.sub(r'^download-free-samsung-(4k-)?frame-tv-arts?-', '', slug)
    return slug.replace("-", " ").strip().title() or "Artwork"


def _parse_items(soup: BeautifulSoup) -> list[dict]:
    """Extract the artworks of a grid page, trying the known selectors in turn."""
    items = []
    for selector in ITEM_SELECTORS:
        items = soup.select(selector)
        if items:
            logger.debug(f"Selector used: {selector} ({len(items)} elements)")
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
    Scrape theframetv.com/arts/ and return the list of available artworks.
    Returns a list of dicts: [{name, thumb_url, full_url, page_url}]
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
                logger.debug(f"Page {page} does not exist (404), end of pagination")
                break
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.warning(f"Error scraping page {page}: {e}")
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

        logger.info(f"  -> {found} artworks found on page {page}")
        if without_image:
            logger.warning(
                f"  -> {without_image} artworks without an image URL on page {page} "
                "(the per-page fallback will be used)"
            )

        if found == 0:
            break

        page += 1

    logger.info(f"Total artworks on theframetv.com: {len(artworks)}")
    return artworks


def _get_fullres_from_page(page_url: str) -> str | None:
    """Fetch the full-res URL from the individual page, stripping the size suffix."""
    try:
        resp = requests.get(page_url, headers=HEADERS, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # The main artwork image carries the wp-post-image class
        # (JetEngine "jet-listing-dynamic-image" widget).
        candidates = soup.select(
            "img.wp-post-image, img.jet-listing-dynamic-image__img"
        ) or soup.find_all("img")

        for img in candidates:
            src = _absolute(_img_url(img))
            if _is_artwork_url(src):
                return _thumb_to_fullres(src)

        # Last resort: the featured image declared in the Open Graph metadata.
        og = soup.select_one("meta[property='og:image']")
        if og:
            src = _absolute((og.get("content") or "").strip())
            if _is_artwork_url(src):
                return _thumb_to_fullres(src)

    except Exception as e:
        logger.warning(f"Could not fetch the full-res image from {page_url}: {e}")

    return None


def load_index(index_file: str) -> list[dict]:
    """
    Load the local artwork index from index_file.
    Returns an empty list when the file is missing, unreadable or corrupt.
    """
    if not os.path.exists(index_file):
        return []

    try:
        with open(index_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        logger.error(f"Index unreadable or corrupt ({index_file}): {e}")
        return []

    if not isinstance(data, list):
        logger.error(f"Unexpected index format ({index_file}): a list was expected")
        return []

    return data


def save_index(artworks: list[dict], index_file: str) -> None:
    """
    Save the local index to index_file atomically
    (write to a temporary file, then os.replace).
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
        logger.error(f"Error writing the index {index_file}: {e}")
        try:
            if os.path.exists(tmp_file):
                os.remove(tmp_file)
        except OSError:
            pass
        raise


def index_age_days(index_file: str) -> float | None:
    """
    Return the age of the index in days (based on the file modification time),
    or None when the file does not exist or is not accessible.
    """
    try:
        mtime = os.path.getmtime(index_file)
    except OSError:
        return None

    return max(0.0, (time.time() - mtime) / 86400.0)


def refresh_index(index_file: str, max_pages: int = DEFAULT_MAX_PAGES) -> list[dict]:
    """
    Refresh the index by scraping the site and save it to index_file.

    Safeguard: when scraping returns no artwork at all while the existing index
    holds some, the old index is kept (a failed scrape must not wipe the
    catalogue).
    """
    artworks = fetch_artwork_list(max_pages=max_pages)
    existing = load_index(index_file)

    if not artworks and existing:
        logger.error(
            f"Scraping theframetv.com returned nothing: keeping the existing "
            f"index ({len(existing)} artworks)"
        )
        return existing

    save_index(artworks, index_file)
    return artworks


def download_image(artwork: dict, cache_dir: str) -> str | None:
    """
    Download the full-res image of an artwork into the local cache.
    The download goes to a .part file that is renamed at the end, so a truncated
    JPEG is never left in the cache.
    Returns the local path of the file, or None on failure.
    """
    os.makedirs(cache_dir, exist_ok=True)

    # File name based on the page slug
    slug = artwork["page_url"].rstrip("/").split("/")[-1]
    local_path = os.path.join(cache_dir, f"{slug}.jpg")

    if os.path.exists(local_path):
        logger.debug(f"Already cached: {local_path}")
        return local_path

    # Try the direct full-res URL
    full_url = artwork.get("full_url", "")

    # No full-res URL, or still a thumbnail: fetch it from the page instead
    if not full_url or re.search(r'-\d+x\d+\.\w+$', full_url):
        full_url = _get_fullres_from_page(artwork["page_url"]) or full_url

    if not full_url:
        logger.warning(f"No full-res URL for: {artwork['name']}")
        return None

    tmp_path = f"{local_path}.part"

    try:
        logger.info(f"Downloading: {artwork['name']}")
        resp = requests.get(full_url, headers=HEADERS, timeout=30, stream=True)
        resp.raise_for_status()

        content_type = (resp.headers.get("Content-Type") or "").lower()
        if content_type and not content_type.startswith("image/"):
            logger.error(
                f"Non-image content ({content_type or 'unknown'}) for {full_url}"
            )
            return None

        written = 0
        with open(tmp_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    written += len(chunk)

        if written == 0:
            logger.error(f"Empty download for {full_url}")
            return None

        os.replace(tmp_path, local_path)
        logger.info(f"  -> Saved: {local_path} ({written} bytes)")
        return local_path

    except requests.RequestException as e:
        logger.error(f"Download error {full_url}: {e}")
        return None
    except OSError as e:
        logger.error(f"Error writing the cache {local_path}: {e}")
        return None
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
