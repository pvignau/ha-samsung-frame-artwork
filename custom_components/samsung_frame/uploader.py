"""
Upload images to a Samsung The Frame TV in Art Mode.
Uses NickWaterton's samsungtvws fork (handles TLS, chunking, Frame 2022/2023).
"""
from __future__ import annotations

import io
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from PIL import Image

from .const import (
    ART_STATE_ART, ART_STATE_BUSY, ART_STATE_UNREACHABLE,
    DEFAULT_MAX_TV_IMAGES, IMAGE_MODES, UPLOAD_CATEGORY,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

MAX_FILE_SIZE = 1_900_000  # ~1.9 MB

# Face detection ("smart" mode). The image is downscaled first: Haar cascades
# do not need full resolution and the cost drops accordingly.
_FACE_DETECT_MAX_DIM = 800
# Faces sit slightly above the middle of the frame rather than dead centre,
# which is how a portrait is normally composed.
_FACE_VERTICAL_ANCHOR = 0.42
_FACE_HORIZONTAL_ANCHOR = 0.5
# Padding around the face bounding box, as a fraction of its size, so the crop
# does not hug foreheads and chins.
_FACE_PADDING = 0.35

# Saliency fallback, used by "smart" when face detection is unavailable (OpenCV
# ships no musllinux wheel, so it cannot be installed on Home Assistant OS).
# The energy map is computed on a small copy; 512 px is ample for choosing a
# crop offset and keeps the whole pass in the millisecond range.
_SALIENCY_MAX_DIM = 512
# Pure "most detailed area" can drift to a corner texture. A mild pull towards
# the centre keeps the framing natural without cancelling the effect.
_SALIENCY_CENTRE_BIAS = 0.25

# Date fields that may appear in the metadata returned by the TV
_DATE_FIELDS = ("image_date", "date", "create_date", "modified_date")
# Date formats seen on the Samsung side (EXIF-like and ISO)
_DATE_FORMATS = (
    "%Y:%m:%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y:%m:%d",
    "%Y-%m-%d",
    "%Y-%m-%dT%H:%M:%S",
)


def prepare_image(image_path: str, width: int = 3840, height: int = 2160,
                  mode: str = "fill", jpeg_quality: int = 85) -> bytes:
    """Resize and convert an image for The Frame TV."""
    if width <= 0 or height <= 0:
        raise ValueError(
            f"Invalid target dimensions: {width}x{height} (must be > 0)"
        )

    img = Image.open(image_path).convert("RGB")
    src_width, src_height = img.width, img.height

    if img.width <= 0 or img.height <= 0:
        raise ValueError(f"Invalid source image: {src_width}x{src_height}")

    if mode not in IMAGE_MODES:
        _LOGGER.warning("Unknown image mode '%s', falling back to 'fill'", mode)
        mode = "fill"

    target_ratio = width / height

    if mode == "fit":
        # Letterbox: the image sits on a black background already at the right size.
        img.thumbnail((width, height), Image.LANCZOS)
        background = Image.new("RGB", (width, height), (0, 0, 0))
        paste_x = (width - img.width) // 2
        paste_y = (height - img.height) // 2
        background.paste(img, (paste_x, paste_y))
        img = background  # already exactly width x height, no final resize
    else:
        # fill / smart: crop to the target ratio, then scale. In smart mode the
        # crop window is anchored on faces instead of being centred.
        if src_width < width or src_height < height:
            _LOGGER.warning(
                "Source image (%dx%d) smaller than the target (%dx%d), "
                "upscaling applied (possible quality loss)",
                src_width, src_height, width, height,
            )
        img = _crop_to_ratio(img, target_ratio, smart=(mode == "smart"))
        img = img.resize((width, height), Image.LANCZOS)

    for quality in (jpeg_quality, 80, 70, 60):
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        data = buf.getvalue()
        if len(data) <= MAX_FILE_SIZE:
            if quality != jpeg_quality:
                _LOGGER.info("Image recompressed at q%d: %d KB", quality, len(data) // 1024)
            return data

    img_half = img.resize((width // 2, height // 2), Image.LANCZOS)
    buf = io.BytesIO()
    img_half.save(buf, format="JPEG", quality=70, optimize=True)
    _LOGGER.warning("Image downscaled to %dx%d", width // 2, height // 2)
    return buf.getvalue()


def _detect_faces(img: Image.Image) -> list[tuple[int, int, int, int]]:
    """
    Detect faces and return their rectangles (x, y, w, h) in the coordinates of
    the original image. Empty list when OpenCV is missing, when detection fails
    or when the image contains nobody.
    """
    try:
        import cv2
        import numpy as np
    except ImportError:
        _LOGGER.warning(
            "opencv-python-headless unavailable: centred crop instead of smart mode"
        )
        return []

    # OpenCV 5 dropped CascadeClassifier and the Haar cascades, so the manifest
    # pins the 4.x branch. Should a 5.x end up installed anyway, say so plainly
    # rather than silently cropping to the centre.
    if not hasattr(cv2, "CascadeClassifier") or not hasattr(cv2, "data"):
        _LOGGER.warning(
            "OpenCV %s does not ship the Haar cascades (4.x branch required): "
            "centred crop instead of smart mode",
            getattr(cv2, "__version__", "?"),
        )
        return []

    try:
        scale = min(1.0, _FACE_DETECT_MAX_DIM / max(img.width, img.height))
        small = img if scale >= 1.0 else img.resize(
            (max(1, int(img.width * scale)), max(1, int(img.height * scale))),
            Image.BILINEAR,
        )
        gray = cv2.cvtColor(np.asarray(small), cv2.COLOR_RGB2GRAY)
        gray = cv2.equalizeHist(gray)

        boxes: list[tuple[int, int, int, int]] = []
        for cascade_name in ("haarcascade_frontalface_default.xml",
                             "haarcascade_profileface.xml"):
            cascade = cv2.CascadeClassifier(cv2.data.haarcascades + cascade_name)
            if cascade.empty():
                continue
            found = cascade.detectMultiScale(
                gray, scaleFactor=1.1, minNeighbors=5, minSize=(24, 24)
            )
            boxes.extend(tuple(int(v) for v in box) for box in found)
            if boxes and cascade_name.startswith("haarcascade_frontalface"):
                break  # frontal is enough, no need to pay for the profile pass

        if not boxes:
            return []

        inv = 1.0 / scale if scale else 1.0
        return [
            (int(x * inv), int(y * inv), int(w * inv), int(h * inv))
            for x, y, w, h in boxes
        ]
    except Exception:  # noqa: BLE001 - detection must never break an upload
        _LOGGER.exception("Face detection failed, cropping to the centre")
        return []


def _saliency_offset(img: Image.Image, crop_w: int, crop_h: int) -> tuple[int, int] | None:
    """
    Position (left, top) of the crop window over the busiest area of the image.

    Used when face detection is unavailable. Cropping to a ratio leaves only one
    axis free, so this is a one-dimensional search: the per-row (or per-column)
    edge energy is summed, then the best sliding window is picked, with a mild
    pull towards the centre. Returns None when numpy is missing or the image is
    degenerate, in which case the caller falls back to a centred crop.
    """
    try:
        import numpy as np
    except ImportError:
        _LOGGER.debug("numpy unavailable, centred crop instead of saliency")
        return None

    free_x = crop_w < img.width
    free_y = crop_h < img.height
    if not free_x and not free_y:
        return 0, 0

    try:
        scale = min(1.0, _SALIENCY_MAX_DIM / max(img.width, img.height))
        small = img if scale >= 1.0 else img.resize(
            (max(2, int(img.width * scale)), max(2, int(img.height * scale))),
            Image.BILINEAR,
        )
        grey = np.asarray(small.convert("L"), dtype=np.float32)
        if grey.shape[0] < 2 or grey.shape[1] < 2:
            return None

        # Edge energy: absolute differences with the neighbour, spread over both
        # pixels of each pair so the map keeps the shape of the image.
        energy = np.zeros_like(grey)
        gx = np.abs(np.diff(grey, axis=1))
        gy = np.abs(np.diff(grey, axis=0))
        energy[:, :-1] += gx
        energy[:, 1:] += gx
        energy[:-1, :] += gy
        energy[1:, :] += gy

        sh, sw = grey.shape
        if free_y:
            profile = energy.sum(axis=1)
            window = max(1, min(sh, round(crop_h * sh / img.height)))
            span, full_span, full_window = sh, img.height, crop_h
        else:
            profile = energy.sum(axis=0)
            window = max(1, min(sw, round(crop_w * sw / img.width)))
            span, full_span, full_window = sw, img.width, crop_w

        # A flat image carries no signal: the centre bias below is
        # multiplicative, so on an all-zero profile argmax would silently
        # return offset 0, i.e. the top or left edge. Fall back to centred.
        if not float(profile.sum()) > 0.0:
            _LOGGER.debug("Uniform image, no salient area, cropping to the centre")
            return None

        if window >= span:
            best_small = 0
        else:
            cumulative = np.concatenate(([0.0], np.cumsum(profile, dtype=np.float64)))
            sums = cumulative[window:] - cumulative[:-window]
            if not float(sums.max()) > 0.0:
                return None
            # Mild centre prior, expressed as a multiplicative weight over the
            # candidate offsets rather than a hard constraint.
            offsets = np.arange(sums.size, dtype=np.float64)
            centres = offsets + window / 2.0
            distance = np.abs(centres - span / 2.0) / (span / 2.0)
            best_small = int(np.argmax(sums * (1.0 - _SALIENCY_CENTRE_BIAS * distance)))

        offset = int(round(best_small * full_span / span))
        offset = max(0, min(offset, full_span - full_window))
        return (0, offset) if free_y else (offset, 0)
    except Exception:  # noqa: BLE001 - cropping must never break an upload
        _LOGGER.exception("Saliency analysis failed, cropping to the centre")
        return None


def _crop_box_for_faces(
    img_w: int, img_h: int, crop_w: int, crop_h: int,
    faces: list[tuple[int, int, int, int]],
) -> tuple[int, int]:
    """Position (left, top) of the crop window that best frames the faces."""
    x0 = min(f[0] for f in faces)
    y0 = min(f[1] for f in faces)
    x1 = max(f[0] + f[2] for f in faces)
    y1 = max(f[1] + f[3] for f in faces)

    pad_x = (x1 - x0) * _FACE_PADDING
    pad_y = (y1 - y0) * _FACE_PADDING
    center_x = (x0 + x1) / 2
    center_y = (y0 + y1) / 2

    # When the face area (padding included) exceeds the window, all we can do is
    # centre on it: keeping everything is impossible.
    left = center_x - crop_w * _FACE_HORIZONTAL_ANCHOR
    top = center_y - crop_h * _FACE_VERTICAL_ANCHOR

    # Pull the window so the padding fits whenever there is room for it.
    left = min(left, x0 - pad_x)
    left = max(left, x1 + pad_x - crop_w)
    top = min(top, y0 - pad_y)
    top = max(top, y1 + pad_y - crop_h)

    left = int(round(max(0, min(left, img_w - crop_w))))
    top = int(round(max(0, min(top, img_h - crop_h))))
    return left, top


def _crop_to_ratio(img: Image.Image, target_ratio: float, smart: bool) -> Image.Image:
    """Crop to the target ratio, centred or anchored on the detected faces."""
    img_ratio = img.width / img.height
    if img_ratio > target_ratio:
        crop_w, crop_h = int(img.height * target_ratio), img.height
    else:
        crop_w, crop_h = img.width, int(img.width / target_ratio)
    crop_w = max(1, min(crop_w, img.width))
    crop_h = max(1, min(crop_h, img.height))

    left = top = None

    if smart:
        faces = _detect_faces(img)
        if faces:
            left, top = _crop_box_for_faces(img.width, img.height, crop_w, crop_h, faces)
            _LOGGER.info(
                "%d face(s) detected, crop anchored on them (offset %d,%d)",
                len(faces), left, top,
            )
        else:
            # No face, or no face detector available: aim at the busiest area
            # rather than blindly at the centre.
            offset = _saliency_offset(img, crop_w, crop_h)
            if offset is not None:
                left, top = offset
                _LOGGER.info("Crop anchored on the busiest area (offset %d,%d)", left, top)

    if left is None or top is None:
        left = (img.width - crop_w) // 2
        top = (img.height - crop_h) // 2

    return img.crop((left, top, left + crop_w, top + crop_h))


def _parse_image_date(item: dict[str, Any]) -> datetime | None:
    """Extract a usable date from the metadata of an image on the TV."""
    for field in _DATE_FIELDS:
        raw = item.get(field)
        if not raw:
            continue
        if isinstance(raw, (int, float)):
            try:
                return datetime.fromtimestamp(float(raw))
            except (ValueError, OSError, OverflowError):
                continue
        if not isinstance(raw, str):
            continue
        value = raw.strip()
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                continue
        try:
            # tzinfo dropped: only naive datetimes are ever compared together
            return datetime.fromisoformat(value).replace(tzinfo=None)
        except ValueError:
            continue
    return None


def _delete_content_ids(tv, content_ids: list[str]) -> int:
    """Delete a list of content_ids from the TV. Returns how many were removed."""
    if not content_ids:
        return 0

    delete_list = getattr(tv, "delete_list", None)
    if callable(delete_list):
        try:
            delete_list(content_ids)
            return len(content_ids)
        except Exception as err:  # noqa: BLE001 - best-effort purge
            _LOGGER.warning("delete_list failed (%s), falling back to single deletes", err)

    delete_one = getattr(tv, "delete", None)
    if not callable(delete_one):
        _LOGGER.warning("No delete API available in samsungtvws")
        return 0

    deleted = 0
    for content_id in content_ids:
        try:
            delete_one(content_id)
            deleted += 1
        except Exception as err:  # noqa: BLE001 - best-effort purge
            _LOGGER.warning("Deleting %s failed: %s", content_id, err)
    return deleted


def _purge_old_images(tv, current_content_id: str, max_tv_images: int) -> int:
    """
    Delete the oldest images of the user category so that only `max_tv_images`
    remain (the current image included).

    Best-effort: every error is logged and never interrupts the upload.
    Returns the number of images actually deleted.
    """
    if max_tv_images <= 0:
        _LOGGER.debug("Purge disabled (max_tv_images=%s)", max_tv_images)
        return 0

    available = getattr(tv, "available", None)
    if not callable(available):
        _LOGGER.warning("'available' API missing from samsungtvws, purge skipped")
        return 0

    try:
        items = available(UPLOAD_CATEGORY)
    except Exception as err:  # noqa: BLE001 - best-effort purge
        _LOGGER.warning("Could not list the images on the TV: %s", err)
        return 0

    if not isinstance(items, list):
        _LOGGER.warning("Unexpected response from available(): %s", type(items).__name__)
        return 0

    # Keep only the entries of the user category, as a safeguard: never the
    # factory Samsung categories.
    user_items: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        category = item.get("category_id") or item.get("category")
        if category is not None and category != UPLOAD_CATEGORY:
            continue
        if item.get("content_id"):
            user_items.append(item)

    _LOGGER.debug("%d image(s) in category %s", len(user_items), UPLOAD_CATEGORY)

    # Sort oldest to newest when every date is usable, otherwise keep the order
    # returned by the TV.
    dates = [_parse_image_date(item) for item in user_items]
    if dates and all(date is not None for date in dates):
        try:
            ordered = [item for _, item in sorted(
                zip(dates, user_items, strict=True), key=lambda pair: pair[0]
            )]
        except TypeError:  # dates not comparable with one another
            _LOGGER.debug("Dates not comparable, keeping the order from the TV")
            ordered = user_items
    else:
        _LOGGER.debug("Dates not usable, keeping the order from the TV")
        ordered = user_items

    # The image that has just been selected is never deleted.
    candidates = [
        item["content_id"] for item in ordered
        if item["content_id"] != current_content_id
    ]
    allowed_others = max(max_tv_images - 1, 0)
    excess = len(candidates) - allowed_others
    if excess <= 0:
        _LOGGER.debug(
            "No purge needed (%d image(s) for a maximum of %d)",
            len(candidates) + 1, max_tv_images,
        )
        return 0

    to_delete = candidates[:excess]
    _LOGGER.info("Purging %d old image(s) from the TV", len(to_delete))
    deleted = _delete_content_ids(tv, to_delete)
    if deleted:
        _LOGGER.info(
            "%d image(s) deleted from the TV (%d kept)",
            deleted, len(candidates) + 1 - deleted,
        )
    else:
        _LOGGER.warning("Purge had no effect: no image deleted")
    return deleted


def _sync_art_state(tv_ip: str, tv_port: int, token_file: str) -> str:
    """
    Determine whether the TV is already showing Art Mode (blocking).

    On a Frame, "art on screen" and "content playing" both report PowerState=on,
    so it is the Art Mode state that matters, not the power state. Art Mode
    active => replacing the artwork is invisible to the user. Art Mode inactive
    => either the TV is being watched or it is fully off; in both cases pushing
    an image would light up or hijack the screen.
    """
    from samsungtvws.art import SamsungTVArt

    tv = SamsungTVArt(host=tv_ip, port=tv_port, token_file=token_file, timeout=15)
    try:
        artmode = tv.get_artmode()
    except Exception as err:  # noqa: BLE001 - TV unreachable, off, timeout...
        _LOGGER.debug("Art Mode state undetermined (%s): %s", tv_ip, err)
        return ART_STATE_UNREACHABLE

    if isinstance(artmode, str) and artmode.strip().lower() == "on":
        return ART_STATE_ART
    return ART_STATE_BUSY


async def get_art_state(hass: HomeAssistant, tv_ip: str, tv_port: int = 8002,
                        token_file: str = "tv_token.txt") -> str:
    """Return ART_STATE_ART, ART_STATE_BUSY or ART_STATE_UNREACHABLE."""
    try:
        return await hass.async_add_executor_job(
            _sync_art_state, tv_ip, tv_port, token_file
        )
    except Exception:  # noqa: BLE001 - must never interrupt a cycle
        _LOGGER.exception("Error while reading the Art Mode state")
        return ART_STATE_UNREACHABLE


def _sync_check(tv_ip: str, tv_port: int, token_file: str) -> bool:
    """Check (blocking) that the TV answers and supports Art Mode."""
    from samsungtvws.art import SamsungTVArt
    tv = SamsungTVArt(host=tv_ip, port=tv_port, token_file=token_file)
    return tv.supported()


def _sync_upload(image_path: str, tv_ip: str, tv_port: int,
                 token_file: str, image_config: dict,
                 max_tv_images: int) -> bool:
    """Prepare, upload and display the image, then purge the old ones (blocking)."""
    from samsungtvws.art import SamsungTVArt

    width = image_config.get("width", 3840)
    height = image_config.get("height", 2160)
    mode = image_config.get("mode", "fill")
    quality = image_config.get("jpeg_quality", 85)

    _LOGGER.info("Preparing image: %s", image_path)
    image_data = prepare_image(image_path, width, height, mode, quality)
    _LOGGER.info("  -> %d KB", len(image_data) // 1024)

    _LOGGER.info("Connecting to the TV: %s:%d", tv_ip, tv_port)
    tv = SamsungTVArt(host=tv_ip, port=tv_port, token_file=token_file, timeout=60)

    if not tv.supported():
        _LOGGER.error("This TV does not support Art Mode")
        return False

    _LOGGER.info("Uploading...")
    content_id = tv.upload(image_data, file_type="jpg", matte="none")
    if not content_id:
        _LOGGER.error("Upload failed (no content_id)")
        return False

    _LOGGER.info("Upload OK: %s", content_id)
    tv.select_image(content_id, category=UPLOAD_CATEGORY, show=True)
    _LOGGER.info("Image displayed on the TV")

    # Best-effort purge: it must never make a successful upload fail.
    try:
        _purge_old_images(tv, content_id, max_tv_images)
    except Exception:  # noqa: BLE001 - best-effort purge
        _LOGGER.exception("Purging old images failed (upload kept)")

    return True


async def upload_to_frame(hass: HomeAssistant, image_path: str, tv_ip: str,
                          tv_port: int = 8002,
                          token_file: str = "tv_token.txt",
                          image_config: dict | None = None,
                          max_tv_images: int = DEFAULT_MAX_TV_IMAGES) -> bool:
    """Push an image to the TV and purge the old ones. True on success."""
    cfg = image_config or {}
    try:
        return await hass.async_add_executor_job(
            _sync_upload, image_path, tv_ip, tv_port, token_file, cfg, max_tv_images
        )
    except Exception:
        _LOGGER.exception("Error uploading to the TV")
        return False


async def check_tv_connection(hass: HomeAssistant, tv_ip: str, tv_port: int = 8002,
                              token_file: str = "tv_token.txt") -> bool:
    """Test the connection to the TV and whether it supports Art Mode."""
    try:
        return await hass.async_add_executor_job(
            _sync_check, tv_ip, tv_port, token_file
        )
    except Exception as e:
        _LOGGER.error("TV unreachable (%s): %s", tv_ip, e)
        return False
