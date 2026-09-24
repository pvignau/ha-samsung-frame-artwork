"""
Source: iCloud shared album
Downloads photos from a public iCloud shared album (no authentication).

To create a shared album:
1. Open the Photos app on iPhone/Mac
2. Album -> New shared album
3. Enable "Public Website"
4. Copy the URL (e.g. https://photos.icloud.com/shared/album/XXXXXXXX)
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

# The partition is not to be guessed: it is encoded in the token itself, in its
# characters at index 1 and 2, in base62. Verified examples:
#   B12GfnH8tC0ZuK -> "12" -> 1*62 + 2  = 64   -> p64-sharedstreams.icloud.com
#   D2Av3xm1...    -> "2A" -> 2*62 + 10 = 134  -> p134-sharedstreams.icloud.com
# Partitions therefore go well beyond p09.
_BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_DEFAULT_PARTITION = 1

# When the derived partition is wrong, Apple answers 330 with the right host in
# X-Apple-MMe-Host. Safeguard against a circular redirect chain.
_MAX_REDIRECTS = 5


# Three shapes of public link depending on the era:
#   https://www.icloud.com/photos/#B0abcdef          (oldest)
#   https://www.icloud.com/sharedalbum/#B0abcdef
#   https://photos.icloud.com/shared/album/B0abcdef  (current)
# The '#' is optional, and the Photos app sometimes appends the album name as a
# second fragment: .../#B0abcdef#Holidays. The token therefore stops at the
# first '#', '/' or '?'. Must stay in sync with ICLOUD_URL_RE in config_flow,
# which validates what the user types.
_TOKEN_RE = re.compile(
    r"/(?:photos|sharedalbum|shared/album)/#?([A-Za-z0-9_-]+)",
    re.IGNORECASE,
)


def extract_token(share_url: str) -> str | None:
    """Extract the share token from the iCloud URL, or None when unreadable."""
    match = _TOKEN_RE.search((share_url or "").strip())
    return match.group(1) if match else None


# Former name, kept for internal compatibility.
_extract_token = extract_token


def _safe_filename(filename: str, fallback: str) -> str:
    """
    Sanitise a file name coming from the iCloud API (an external source).
    Prevents any path traversal ('/', '\\', '..') outside the cache directory.
    """
    # Keep only the final component, whichever separator was used.
    name = str(filename or "").replace("\\", "/").split("/")[-1]
    name = os.path.basename(name).strip()

    # Neutralise problematic characters and special names.
    name = re.sub(r'[^A-Za-z0-9._-]', "_", name)
    name = name.lstrip(".")

    if not name or name in (".", ".."):
        name = re.sub(r'[^A-Za-z0-9._-]', "_", str(fallback or "photo")) or "photo"

    # Leave headroom below the usual 255-byte file name limit.
    if len(name) > 200:
        root, ext = os.path.splitext(name)
        name = root[:200 - len(ext)] + ext

    return name


def _partition_from_token(token: str) -> int:
    """Derive the partition number encoded in the token (base62 of chars 1 and 2)."""
    if len(token) < 3:
        return _DEFAULT_PARTITION
    value = 0
    for char in token[1:3]:
        index = _BASE62.find(char)
        if index < 0:
            logger.debug(f"Non-base62 character in the token: {char!r}")
            return _DEFAULT_PARTITION
        value = value * 62 + index
    return value or _DEFAULT_PARTITION


def _base_url(partition: int) -> str:
    return ICLOUD_API_BASE.format(partition=partition)


def _host_to_base_url(host: str) -> str | None:
    """Turn the host returned by a 330 redirect into a base URL."""
    host = (host or "").strip().strip("/")
    if host.startswith("http://") or host.startswith("https://"):
        host = host.split("//", 1)[1]
    if not re.fullmatch(r"p\d+-sharedstreams\.icloud\.com", host, re.IGNORECASE):
        return None
    return f"https://{host}"


def fetch_album_metadata(share_url: str) -> dict | None:
    """
    Fetch the metadata of the iCloud shared album.
    Returns a dict with the album information, or None on error.

    The partition is derived from the token; when it is wrong, Apple answers
    HTTP 330 with the right host in X-Apple-MMe-Host, which is then followed.
    """
    token = extract_token(share_url)
    if not token:
        logger.error(f"Invalid iCloud URL: {share_url}")
        return None

    base_url = _base_url(_partition_from_token(token))
    logger.debug(f"Partition derived from the token: {base_url}")
    tried: set[str] = set()

    for _ in range(_MAX_REDIRECTS):
        if base_url in tried:
            logger.debug(f"Host already tried, stopping: {base_url}")
            break
        tried.add(base_url)

        api_url = f"{base_url}/{token}/sharedstreams/webstream"
        try:
            resp = requests.post(
                api_url, headers=HEADERS, json={"streamCtag": None}, timeout=10
            )
        except requests.RequestException as e:
            logger.warning(f"iCloud album unreachable on {base_url}: {e}")
            return None

        if resp.status_code == 200:
            try:
                payload = resp.json()
            except ValueError as e:
                logger.warning(f"Unreadable iCloud response ({base_url}): {e}")
                return None
            logger.info(f"iCloud album found on {base_url}")
            return {"token": token, "base_url": base_url, **payload}

        if resp.status_code == 330:
            try:
                data = resp.json()
            except ValueError:
                data = {}
            host = data.get("X-Apple-MMe-Host") or resp.headers.get("X-Apple-MMe-Host")
            target = _host_to_base_url(host) if host else None
            if not target:
                logger.warning(f"Unreadable iCloud redirect: {host!r}")
                return None
            logger.info(f"iCloud redirect to {target}")
            base_url = target
            continue

        if resp.status_code == 404:
            logger.error(
                "iCloud album not found (404): the share link may have expired, "
                "or \"Public Website\" is not enabled on the album."
            )
            return None

        logger.warning(
            f"Unexpected HTTP status {resp.status_code} from {base_url} for the iCloud album"
        )
        return None

    logger.error(f"Could not reach the iCloud album after redirects: {share_url}")
    return None


def _fetch_photo_list_sharedstreams(share_url: str) -> list[dict]:
    """
    Return the list of photos of the shared album.
    Each photo is a dict: {guid, filename, url, width, height, created}
    """
    meta = fetch_album_metadata(share_url)
    if not meta:
        return []

    token = meta["token"]
    base_url = meta["base_url"]
    photos_raw = meta.get("photos", [])

    if not photos_raw:
        logger.warning("Empty album, or unexpected format")
        return []

    # Fetch the download URLs
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
                f"Unexpected HTTP status {resp.status_code} while fetching the "
                "iCloud asset URLs"
            )
        resp.raise_for_status()
        asset_data = resp.json()
    except requests.RequestException as e:
        logger.error(f"Error fetching the iCloud asset URLs: {e}")
        return []
    except ValueError as e:
        logger.error(f"Unreadable iCloud response (asset URLs): {e}")
        return []

    photos = []
    items = asset_data.get("items", {})

    for photo in photos_raw:
        guid = photo.get("photoGuid")
        derivatives = photo.get("derivatives", {})

        # Take the highest resolution available
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

    logger.info(f"iCloud album: {len(photos)} photos found")
    return photos


# ─────────────────────────────────────────────────────────────────────────────
# Modern shared albums (CloudKit)
#
# Recent links (photos.icloud.com/shared/album/...) are no longer served by the
# sharedstreams API: Apple resolves them through CloudKit. Sequence observed on
# the official web client, in anonymous access:
#   1. POST ckdatabasews.icloud.com/.../public/records/resolve?sharing_url_key=TOKEN
#      body {"shortGUIDs":[{"value":TOKEN}]}
#      -> results[0].anonymousPublicAccess = {token, tokenTTL, databasePartition}
#         results[0].zoneID                = the album zone
#   2. POST <databasePartition>/.../shared/changes/zone?publicAccessAuthToken=...
#      body {"zones":[{"zoneID":...}]}  (no syncToken = the whole content)
#      -> CPLMaster records carrying the signed download URLs
#
# Undocumented API, obtained by observation: it may change without notice.
# That is why the former implementation is kept as a fallback.
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
# Pillow cannot read HEIC without a plugin, so the original is only used when it
# already is in a safe format; otherwise Apple's own JPEG derivative is taken.
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
    """Raw value of a CloudKit field."""
    entry = fields.get(name)
    return entry.get("value", default) if isinstance(entry, dict) else default


def _ck_decode_filename(fields: dict, fallback: str) -> str:
    """filenameEnc holds the file name, base64-encoded."""
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
    """Resolve the share link: anonymous token, partition and album zone."""
    try:
        resp = requests.post(
            f"{_CK_BASE}/public/records/resolve",
            params=_ck_params(token),
            headers=_CK_HEADERS,
            data=json.dumps({"shortGUIDs": [{"value": token}]}),
            timeout=15,
        )
    except requests.RequestException as e:
        logger.debug(f"CloudKit unreachable: {e}")
        return None

    if resp.status_code != 200:
        logger.debug(f"CloudKit resolve answered {resp.status_code}")
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
        logger.warning(f"Unexpected CloudKit response: {e}")
        return None

    if not partition.startswith("https://"):
        logger.warning(f"Invalid CloudKit partition: {partition!r}")
        return None

    logger.info(f"iCloud album \"{resolved['title']}\" resolved through CloudKit ({partition})")
    return resolved


def _ck_pick_resource(fields: dict) -> tuple[dict, int, int] | None:
    """Pick the best resource Pillow can read. (resource, width, height)"""
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
    """Enumerate the photos of the shared zone (pagination through syncToken)."""
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
            logger.warning(f"CloudKit listing interrupted: {e}")
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
            # The JPEG derivative of a HEIC original keeps a .HEIC name: restore
            # the real extension so the cache is not misled.
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

    logger.info(f"iCloud album (CloudKit): {len(photos)} usable photo(s)")
    return photos


def fetch_photo_list(share_url: str) -> list[dict]:
    """
    Return the list of photos of the shared album.
    Each photo is a dict: {guid, filename, url, width, height, created}

    Tries CloudKit first (recent links), then the former sharedstreams API.
    """
    token = extract_token(share_url)
    if not token:
        logger.error(f"Invalid iCloud URL: {share_url}")
        return []

    resolved = _ck_resolve(token)
    if resolved:
        photos = _ck_list_photos(token, resolved)
        if photos:
            return photos
        logger.warning("CloudKit album resolved but no usable photo found")
        return []

    logger.debug("CloudKit did not resolve this link, trying the sharedstreams API")
    return _fetch_photo_list_sharedstreams(share_url)


def _looks_like_image(head: bytes) -> bool:
    """Recognise an image from its signature, without trusting the Content-Type."""
    return (
        head.startswith(b"\xff\xd8\xff")                      # JPEG
        or head.startswith(b"\x89PNG\r\n\x1a\n")              # PNG
        or head.startswith(b"GIF87a") or head.startswith(b"GIF89a")
        or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")     # WebP
        or head[4:12] in (b"ftypheic", b"ftypheix", b"ftyphevc", b"ftypmif1")  # HEIF
    )


def download_image(photo: dict, cache_dir: str) -> str | None:
    """
    Download an iCloud photo into the local cache.
    The file name coming from the iCloud API is sanitised (no path traversal),
    and the download goes through a .part file renamed at the end.
    Returns the local path, or None on failure.
    """
    os.makedirs(cache_dir, exist_ok=True)

    guid = photo.get("guid") or "photo"
    filename = _safe_filename(photo.get("filename", ""), f"{guid}.jpg")
    local_path = os.path.join(cache_dir, filename)

    if os.path.exists(local_path):
        logger.debug(f"Already cached: {local_path}")
        return local_path

    url = photo.get("url", "")
    if not url:
        logger.warning(f"No URL for photo {photo.get('guid')}")
        return None

    tmp_path = f"{local_path}.part"

    try:
        logger.info(f"Downloading from iCloud: {filename}")
        resp = requests.get(url, timeout=30, stream=True)
        resp.raise_for_status()

        # Apple's CDN serves photos as application/octet-stream, so trusting the
        # Content-Type would reject perfectly valid images. The real signature of
        # the first bytes is checked instead, which is more reliable.
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
            logger.error(f"Empty download for photo {filename}")
            return None

        if not _looks_like_image(head):
            logger.error(
                f"Content not recognised as an image for photo {filename} "
                f"(first bytes: {head[:4].hex()}, {written} bytes)"
            )
            return None

        os.replace(tmp_path, local_path)
        logger.debug(f"  -> Saved: {local_path} ({written} bytes)")
        return local_path

    except requests.RequestException as e:
        logger.error(f"iCloud download error {filename}: {e}")
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
