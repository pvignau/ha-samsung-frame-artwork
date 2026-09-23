"""
Upload d'images vers Samsung The Frame en mode Art.
Utilise le fork NickWaterton de samsungtvws (gère TLS, chunking, Frame 2022/2023).
"""
from __future__ import annotations

import asyncio
import io
import logging
from functools import partial

from PIL import Image

_LOGGER = logging.getLogger(__name__)

MAX_FILE_SIZE = 1_900_000  # ~1.9 MB


def prepare_image(image_path: str, width: int = 3840, height: int = 2160,
                  mode: str = "fill", jpeg_quality: int = 85) -> bytes:
    """Redimensionne et convertit une image pour The Frame TV."""
    img = Image.open(image_path).convert("RGB")
    target_ratio = width / height
    img_ratio = img.width / img.height

    if mode == "fill":
        if img_ratio > target_ratio:
            new_width = int(img.height * target_ratio)
            offset = (img.width - new_width) // 2
            img = img.crop((offset, 0, offset + new_width, img.height))
        else:
            new_height = int(img.width / target_ratio)
            offset = (img.height - new_height) // 2
            img = img.crop((0, offset, img.width, offset + new_height))
    elif mode == "fit":
        img.thumbnail((width, height), Image.LANCZOS)
        background = Image.new("RGB", (width, height), (0, 0, 0))
        paste_x = (width - img.width) // 2
        paste_y = (height - img.height) // 2
        background.paste(img, (paste_x, paste_y))
        img = background

    img = img.resize((width, height), Image.LANCZOS)

    for quality in (jpeg_quality, 80, 70, 60):
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        data = buf.getvalue()
        if len(data) <= MAX_FILE_SIZE:
            if quality != jpeg_quality:
                _LOGGER.info("Image recompressée à q%d: %d Ko", quality, len(data) // 1024)
            return data

    img_half = img.resize((width // 2, height // 2), Image.LANCZOS)
    buf = io.BytesIO()
    img_half.save(buf, format="JPEG", quality=70, optimize=True)
    _LOGGER.warning("Image réduite à %dx%d", width // 2, height // 2)
    return buf.getvalue()


def _sync_check(tv_ip: str, tv_port: int, token_file: str) -> bool:
    from samsungtvws.art import SamsungTVArt
    tv = SamsungTVArt(host=tv_ip, port=tv_port, token_file=token_file)
    return tv.supported()


def _sync_upload(image_path: str, tv_ip: str, tv_port: int,
                 token_file: str, image_config: dict) -> bool:
    from samsungtvws.art import SamsungTVArt

    width = image_config.get("width", 3840)
    height = image_config.get("height", 2160)
    mode = image_config.get("mode", "fill")
    quality = image_config.get("jpeg_quality", 85)

    _LOGGER.info("Préparation de l'image: %s", image_path)
    image_data = prepare_image(image_path, width, height, mode, quality)
    _LOGGER.info("  → %d Ko", len(image_data) // 1024)

    _LOGGER.info("Connexion à la TV: %s:%d", tv_ip, tv_port)
    tv = SamsungTVArt(host=tv_ip, port=tv_port, token_file=token_file, timeout=60)

    if not tv.supported():
        _LOGGER.error("Cette TV ne supporte pas le mode Art")
        return False

    _LOGGER.info("Upload en cours...")
    content_id = tv.upload(image_data, file_type="jpg", matte="none")
    if not content_id:
        _LOGGER.error("Upload échoué (pas de content_id)")
        return False

    _LOGGER.info("Upload OK: %s", content_id)
    tv.select_image(content_id, category="MY-C0002", show=True)
    _LOGGER.info("Image affichée sur la TV !")
    return True


async def upload_to_frame(image_path: str, tv_ip: str, tv_port: int = 8002,
                          token_file: str = "tv_token.txt",
                          image_config: dict = None) -> bool:
    cfg = image_config or {}
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(
            None,
            partial(_sync_upload, image_path, tv_ip, tv_port, token_file, cfg),
        )
    except Exception:
        _LOGGER.exception("Erreur upload vers la TV")
        return False


async def check_tv_connection(tv_ip: str, tv_port: int = 8002,
                              token_file: str = "tv_token.txt") -> bool:
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(
            None,
            partial(_sync_check, tv_ip, tv_port, token_file),
        )
    except Exception as e:
        _LOGGER.error("TV inaccessible (%s): %s", tv_ip, e)
        return False
