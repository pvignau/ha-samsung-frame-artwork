"""
Source: custom URLs
Images from any URL the user configures, the way WallPanel's image_url works.

One URL per line, optionally followed by a JSON path:

    https://picsum.photos/${width}/${height}?random=${timestamp}
    https://api.unsplash.com/photos/random?client_id=KEY&count=30 urls.full

- When the URL answers with an image, that image is used as is.
- When it answers with JSON, the image URLs are read at the JSON path, or at a
  few common locations when no path is given. Lists met along the path are
  walked through, so "hits.largeImageURL" reads every hit.

${width}, ${height} and ${timestamp} are replaced before the request.
Respecting the terms of each service is up to whoever configures it.
"""

from __future__ import annotations

import os
import json
import time
import random
import hashlib
import logging
from urllib.parse import urlsplit

import requests

logger = logging.getLogger(__name__)

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; FrameUpdater/1.0)"}

# Larger answers are refused: a misconfigured URL must not fill the disk.
MAX_JSON_BYTES = 5 * 1024 * 1024
MAX_IMAGE_BYTES = 50 * 1024 * 1024

# Tried in order when a JSON answer comes without a path.
COMMON_PATHS = (
    "urls.full",            # Unsplash
    "hdurl",                # NASA APOD
    "primaryImage",         # Metropolitan Museum object
    "largeImageURL",        # Pixabay hit
    "hits.largeImageURL",   # Pixabay search
    "src.original",         # Pexels photo
    "photos.src.original",  # Pexels search
    "url",
    "image",
)

# Metadata read next to the image URL, first match wins.
_TITLE_PATHS = ("title", "description", "alt_description")
_AUTHOR_PATHS = ("user.name", "photographer", "artistDisplayName", "author", "copyright", "user")


def parse_lines(text: str) -> list[tuple[str, str | None]]:
    """Split the option into (url, json path or None), skipping blank lines."""
    entries = []
    for line in (text or "").splitlines():
        parts = line.split()
        if parts:
            entries.append((parts[0], parts[1] if len(parts) > 1 else None))
    return entries


def is_valid_url(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def _fill(url: str, width: int, height: int) -> str:
    return (
        url.replace("${width}", str(width))
        .replace("${height}", str(height))
        .replace("${timestamp}", str(int(time.time())))
    )


def _redact(url: str) -> str:
    """Drop the query string, which often carries an API key, before logging."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def _resolve(node, keys: list[str], item) -> list[tuple[str, object]]:
    """Values found at ``keys``, each paired with the list element it came from."""
    if isinstance(node, list):
        return [found for element in node for found in _resolve(element, keys, element)]
    if not keys:
        return [(node, item)] if isinstance(node, str) else []
    if isinstance(node, dict) and keys[0] in node:
        return _resolve(node[keys[0]], keys[1:], item)
    return []


def _first_text(item, paths: tuple[str, ...]) -> str:
    for path in paths:
        for value, _ in _resolve(item, path.split("."), item):
            if value.strip():
                return value.strip()
    return ""


def _image_candidates(data, path: str | None) -> list[dict]:
    for candidate_path in (path,) if path else COMMON_PATHS:
        found = [
            (value, item) for value, item in _resolve(data, candidate_path.split("."), data)
            if is_valid_url(value)
        ]
        if found:
            return [
                {
                    "url": value,
                    "title": _first_text(item, _TITLE_PATHS),
                    "author": _first_text(item, _AUTHOR_PATHS),
                }
                for value, item in found
            ]
    return []


def _read_capped(resp, limit: int) -> bytes | None:
    body = b""
    for chunk in resp.iter_content(chunk_size=65536):
        body += chunk
        if len(body) > limit:
            return None
    return body


def _save(body: bytes, cache_dir: str, key: str) -> str | None:
    """Store an image under a name derived from ``key``, atomically."""
    os.makedirs(cache_dir, exist_ok=True)
    local_path = os.path.join(cache_dir, hashlib.sha1(key.encode()).hexdigest()[:20] + ".img")
    tmp_path = f"{local_path}.part"
    try:
        with open(tmp_path, "wb") as f:
            f.write(body)
        os.replace(tmp_path, local_path)
        return local_path
    except OSError as e:
        logger.error(f"Error writing the cache {local_path}: {e}")
        return None
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _download(url: str) -> bytes | None:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30, stream=True)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error(f"Custom URL download error {_redact(url)}: {e}")
        return None
    if not (resp.headers.get("Content-Type") or "").lower().startswith("image/"):
        logger.error(f"Not an image: {_redact(url)} ({resp.headers.get('Content-Type')})")
        return None
    body = _read_capped(resp, MAX_IMAGE_BYTES)
    if not body:
        logger.error(f"Image empty or above {MAX_IMAGE_BYTES // 1048576} MB: {_redact(url)}")
    return body


def _from_entry(
    url: str, path: str | None, cache_dir: str, history: list[str], width: int, height: int
) -> dict | None:
    url = _fill(url, width, height)
    host = urlsplit(url).netloc
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30, stream=True)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error(f"Custom URL error {_redact(url)}: {e}")
        return None

    if (resp.headers.get("Content-Type") or "").lower().startswith("image/"):
        body = _read_capped(resp, MAX_IMAGE_BYTES)
        if not body:
            logger.error(f"Image empty or above {MAX_IMAGE_BYTES // 1048576} MB: {_redact(url)}")
            return None
        # The URL is the same at every draw: identify the image by its content.
        digest = hashlib.sha1(body).hexdigest()
        image_path = _save(body, cache_dir, digest)
        if not image_path:
            return None
        return {"path": image_path, "name": f"Image from {host}", "source_id": digest}

    body = _read_capped(resp, MAX_JSON_BYTES)
    if body is None:
        logger.error(f"Answer above {MAX_JSON_BYTES // 1048576} MB: {_redact(url)}")
        return None
    try:
        data = json.loads(body)
    except ValueError:
        logger.error(f"Neither an image nor JSON: {_redact(url)} ({resp.headers.get('Content-Type')})")
        return None

    candidates = _image_candidates(data, path)
    if not candidates:
        logger.error(
            f"No image URL found in the JSON of {_redact(url)}"
            + (f" at '{path}'" if path else ": add the JSON path after the URL")
        )
        return None

    avoid = set(history)
    photo = random.choice([c for c in candidates if c["url"] not in avoid] or candidates)
    body = _download(photo["url"])
    if not body:
        return None
    image_path = _save(body, cache_dir, photo["url"])
    if not image_path:
        return None

    name = photo["title"] or f"Image from {host}"
    if photo["author"]:
        name = f"{name} – {photo['author']}"
    return {
        "path": image_path,
        "name": name,
        "source_id": photo["url"],
        "title": photo["title"],
        "author": photo["author"],
    }


def get_image(
    entries: list[tuple[str, str | None]],
    cache_dir: str,
    history: list[str],
    width: int,
    height: int,
) -> dict | None:
    """Draw a configured URL at random, then the others if it fails."""
    for url, path in random.sample(entries, len(entries)):
        result = _from_entry(url, path, cache_dir, history, width, height)
        if result:
            return result
    return None
