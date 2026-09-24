"""
Upload d'images vers Samsung The Frame en mode Art.
Utilise le fork NickWaterton de samsungtvws (gère TLS, chunking, Frame 2022/2023).
"""
from __future__ import annotations

import io
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from PIL import Image

from .const import (
    ART_STATE_ART, ART_STATE_BUSY, ART_STATE_UNREACHABLE,
    DEFAULT_MAX_TV_IMAGES, UPLOAD_CATEGORY,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

MAX_FILE_SIZE = 1_900_000  # ~1.9 MB

# Champs de date possibles dans les métadonnées renvoyées par la TV
_DATE_FIELDS = ("image_date", "date", "create_date", "modified_date")
# Formats de date rencontrés côté Samsung (EXIF-like et ISO)
_DATE_FORMATS = (
    "%Y:%m:%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y:%m:%d",
    "%Y-%m-%d",
    "%Y-%m-%dT%H:%M:%S",
)


def prepare_image(image_path: str, width: int = 3840, height: int = 2160,
                  mode: str = "fill", jpeg_quality: int = 85) -> bytes:
    """Redimensionne et convertit une image pour The Frame TV."""
    if width <= 0 or height <= 0:
        raise ValueError(
            f"Dimensions cibles invalides: {width}x{height} (doivent être > 0)"
        )

    img = Image.open(image_path).convert("RGB")
    src_width, src_height = img.width, img.height

    if img.width <= 0 or img.height <= 0:
        raise ValueError(f"Image source invalide: {src_width}x{src_height}")

    if mode not in ("fill", "fit"):
        _LOGGER.warning("Mode d'image inconnu '%s', repli sur 'fill'", mode)
        mode = "fill"

    target_ratio = width / height
    img_ratio = img.width / img.height

    if mode == "fit":
        # Lettrebox: l'image est contenue dans un fond noir déjà à la bonne taille.
        img.thumbnail((width, height), Image.LANCZOS)
        background = Image.new("RGB", (width, height), (0, 0, 0))
        paste_x = (width - img.width) // 2
        paste_y = (height - img.height) // 2
        background.paste(img, (paste_x, paste_y))
        img = background  # déjà exactement width x height, pas de resize final
    else:
        # fill: recadrage centré au bon ratio puis mise à l'échelle.
        if src_width < width or src_height < height:
            _LOGGER.warning(
                "Image source (%dx%d) plus petite que la cible (%dx%d), "
                "upscaling appliqué (perte de qualité possible)",
                src_width, src_height, width, height,
            )
        if img_ratio > target_ratio:
            new_width = int(img.height * target_ratio)
            offset = (img.width - new_width) // 2
            img = img.crop((offset, 0, offset + new_width, img.height))
        else:
            new_height = int(img.width / target_ratio)
            offset = (img.height - new_height) // 2
            img = img.crop((0, offset, img.width, offset + new_height))
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


def _parse_image_date(item: dict[str, Any]) -> datetime | None:
    """Extrait une date exploitable des métadonnées d'une image de la TV."""
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
            # tzinfo retiré: on ne compare que des datetime naïfs entre eux
            return datetime.fromisoformat(value).replace(tzinfo=None)
        except ValueError:
            continue
    return None


def _delete_content_ids(tv, content_ids: list[str]) -> int:
    """Supprime une liste de content_id sur la TV. Retourne le nombre supprimé."""
    if not content_ids:
        return 0

    delete_list = getattr(tv, "delete_list", None)
    if callable(delete_list):
        try:
            delete_list(content_ids)
            return len(content_ids)
        except Exception as err:  # noqa: BLE001 - purge best-effort
            _LOGGER.warning("delete_list a échoué (%s), repli sur delete unitaire", err)

    delete_one = getattr(tv, "delete", None)
    if not callable(delete_one):
        _LOGGER.warning("Aucune API de suppression disponible dans samsungtvws")
        return 0

    deleted = 0
    for content_id in content_ids:
        try:
            delete_one(content_id)
            deleted += 1
        except Exception as err:  # noqa: BLE001 - purge best-effort
            _LOGGER.warning("Suppression de %s échouée: %s", content_id, err)
    return deleted


def _purge_old_images(tv, current_content_id: str, max_tv_images: int) -> int:
    """
    Supprime les plus anciennes images de la catégorie utilisateur pour ne garder
    que `max_tv_images` images (image courante incluse).

    Best-effort: toute erreur est logguée et n'interrompt jamais l'upload.
    Retourne le nombre d'images effectivement supprimées.
    """
    if max_tv_images <= 0:
        _LOGGER.debug("Purge désactivée (max_tv_images=%s)", max_tv_images)
        return 0

    available = getattr(tv, "available", None)
    if not callable(available):
        _LOGGER.warning("API 'available' absente de samsungtvws, purge ignorée")
        return 0

    try:
        items = available(UPLOAD_CATEGORY)
    except Exception as err:  # noqa: BLE001 - purge best-effort
        _LOGGER.warning("Impossible de lister les images de la TV: %s", err)
        return 0

    if not isinstance(items, list):
        _LOGGER.warning("Réponse inattendue de available(): %s", type(items).__name__)
        return 0

    # On ne garde que les entrées de la catégorie utilisateur, par sécurité:
    # jamais les catégories Samsung d'origine.
    user_items: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        category = item.get("category_id") or item.get("category")
        if category is not None and category != UPLOAD_CATEGORY:
            continue
        if item.get("content_id"):
            user_items.append(item)

    _LOGGER.debug("%d image(s) dans la catégorie %s", len(user_items), UPLOAD_CATEGORY)

    # Tri du plus ancien au plus récent si toutes les dates sont exploitables,
    # sinon on conserve l'ordre renvoyé par la TV.
    dates = [_parse_image_date(item) for item in user_items]
    if dates and all(date is not None for date in dates):
        try:
            ordered = [item for _, item in sorted(
                zip(dates, user_items, strict=True), key=lambda pair: pair[0]
            )]
        except TypeError:  # dates non comparables entre elles
            _LOGGER.debug("Dates non comparables, conservation de l'ordre de la TV")
            ordered = user_items
    else:
        _LOGGER.debug("Dates non exploitables, conservation de l'ordre de la TV")
        ordered = user_items

    # On ne supprime jamais l'image qui vient d'être sélectionnée.
    candidates = [
        item["content_id"] for item in ordered
        if item["content_id"] != current_content_id
    ]
    allowed_others = max(max_tv_images - 1, 0)
    excess = len(candidates) - allowed_others
    if excess <= 0:
        _LOGGER.debug(
            "Aucune purge nécessaire (%d image(s) pour un maximum de %d)",
            len(candidates) + 1, max_tv_images,
        )
        return 0

    to_delete = candidates[:excess]
    _LOGGER.info("Purge de %d ancienne(s) image(s) sur la TV", len(to_delete))
    deleted = _delete_content_ids(tv, to_delete)
    if deleted:
        _LOGGER.info(
            "%d image(s) supprimée(s) de la TV (%d conservée(s))",
            deleted, len(candidates) + 1 - deleted,
        )
    else:
        _LOGGER.warning("Purge sans effet: aucune image supprimée")
    return deleted


def _sync_art_state(tv_ip: str, tv_port: int, token_file: str) -> str:
    """
    Determine si la TV affiche deja le mode Art (bloquant).

    Sur une Frame, « art affiche » et « contenu en cours » remontent tous deux
    PowerState=on : c'est donc l'etat du mode Art qui fait foi, pas
    l'alimentation. Mode Art actif => remplacer l'oeuvre est invisible pour
    l'utilisateur. Mode Art inactif => soit la TV est regardee, soit elle est
    completement eteinte ; dans les deux cas pousser une image allumerait ou
    detournerait l'ecran.
    """
    from samsungtvws.art import SamsungTVArt

    tv = SamsungTVArt(host=tv_ip, port=tv_port, token_file=token_file, timeout=15)
    try:
        artmode = tv.get_artmode()
    except Exception as err:  # noqa: BLE001 - TV injoignable, eteinte, timeout...
        _LOGGER.debug("Etat du mode Art indeterminable (%s): %s", tv_ip, err)
        return ART_STATE_UNREACHABLE

    if isinstance(artmode, str) and artmode.strip().lower() == "on":
        return ART_STATE_ART
    return ART_STATE_BUSY


async def get_art_state(hass: HomeAssistant, tv_ip: str, tv_port: int = 8002,
                        token_file: str = "tv_token.txt") -> str:
    """Retourne ART_STATE_ART, ART_STATE_BUSY ou ART_STATE_UNREACHABLE."""
    try:
        return await hass.async_add_executor_job(
            _sync_art_state, tv_ip, tv_port, token_file
        )
    except Exception:  # noqa: BLE001 - ne doit jamais interrompre un cycle
        _LOGGER.exception("Erreur lors de la lecture de l'etat du mode Art")
        return ART_STATE_UNREACHABLE


def _sync_check(tv_ip: str, tv_port: int, token_file: str) -> bool:
    """Vérifie (bloquant) que la TV répond et supporte le mode Art."""
    from samsungtvws.art import SamsungTVArt
    tv = SamsungTVArt(host=tv_ip, port=tv_port, token_file=token_file)
    return tv.supported()


def _sync_upload(image_path: str, tv_ip: str, tv_port: int,
                 token_file: str, image_config: dict,
                 max_tv_images: int) -> bool:
    """Prépare, envoie, affiche l'image puis purge les anciennes (bloquant)."""
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
    tv.select_image(content_id, category=UPLOAD_CATEGORY, show=True)
    _LOGGER.info("Image affichée sur la TV !")

    # Purge best-effort: ne doit jamais faire échouer un upload réussi.
    try:
        _purge_old_images(tv, content_id, max_tv_images)
    except Exception:  # noqa: BLE001 - purge best-effort
        _LOGGER.exception("Purge des anciennes images échouée (upload conservé)")

    return True


async def upload_to_frame(hass: HomeAssistant, image_path: str, tv_ip: str,
                          tv_port: int = 8002,
                          token_file: str = "tv_token.txt",
                          image_config: dict | None = None,
                          max_tv_images: int = DEFAULT_MAX_TV_IMAGES) -> bool:
    """Envoie une image sur la TV et purge les anciennes. True si succès."""
    cfg = image_config or {}
    try:
        return await hass.async_add_executor_job(
            _sync_upload, image_path, tv_ip, tv_port, token_file, cfg, max_tv_images
        )
    except Exception:
        _LOGGER.exception("Erreur upload vers la TV")
        return False


async def check_tv_connection(hass: HomeAssistant, tv_ip: str, tv_port: int = 8002,
                              token_file: str = "tv_token.txt") -> bool:
    """Teste la connexion à la TV et le support du mode Art."""
    try:
        return await hass.async_add_executor_job(
            _sync_check, tv_ip, tv_port, token_file
        )
    except Exception as e:
        _LOGGER.error("TV inaccessible (%s): %s", tv_ip, e)
        return False
