"""Coordinator for Samsung Frame – manages artwork rotation."""
from __future__ import annotations

import os
import re
import random
import logging
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN, STORAGE_VERSION,
    CONF_TV_IP, CONF_TV_PORT,
    CONF_THEFRAMETV_ENABLED, CONF_THEFRAMETV_WEIGHT,
    CONF_ICLOUD_ENABLED, CONF_ICLOUD_WEIGHT, CONF_ICLOUD_URL,
    CONF_INTERVAL_HOURS, CONF_HISTORY_SIZE,
    CONF_IMAGE_MODE, CONF_IMAGE_WIDTH, CONF_IMAGE_HEIGHT,
    CONF_MAX_TV_IMAGES, CONF_CACHE_MAX_MB, CONF_INDEX_REFRESH_DAYS,
    CONF_SKIP_WHEN_WATCHING,
    DEFAULT_TV_PORT, DEFAULT_THEFRAMETV_WEIGHT, DEFAULT_ICLOUD_WEIGHT,
    DEFAULT_INTERVAL_HOURS, DEFAULT_HISTORY_SIZE,
    DEFAULT_IMAGE_MODE, DEFAULT_IMAGE_WIDTH, DEFAULT_IMAGE_HEIGHT,
    DEFAULT_MAX_TV_IMAGES, DEFAULT_CACHE_MAX_MB, DEFAULT_INDEX_REFRESH_DAYS,
    DEFAULT_SKIP_WHEN_WATCHING, RETRY_WHEN_BUSY_MINUTES,
    ART_STATE_BUSY, ART_STATE_UNREACHABLE,
)
from .sources import theframetv, icloud
from . import uploader

_LOGGER = logging.getLogger(__name__)

# Maximum length of an HA state string is 255 – keep a small safety margin.
MAX_NAME_LENGTH = 250

# Scraped artwork titles are prefixed with SEO noise such as
# "Download Free Samsung 4K Frame TV Arts - <real title>".
_NAME_PREFIX_RE = re.compile(
    r"^[\s\-_]*download[\s\-_]+free[\s\-_]+samsung[\s\-_]*"
    r"(?:4[\s\-_]*k[\s\-_]*)?frame[\s\-_]*tv[\s\-_]+art(?:works?|s)?\b[\s\-_:.|–—]*",
    re.IGNORECASE,
)


class SamsungFrameCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetches a new artwork and pushes it to the Frame TV on the configured schedule."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._entry = entry
        self._store: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}_{entry.entry_id}")
        # Avoid uploading artwork on the very first HA startup refresh
        self._first_refresh = True

        cfg = self._merged_config
        interval_hours = cfg.get(CONF_INTERVAL_HOURS, DEFAULT_INTERVAL_HOURS)
        # Nominal rotation interval, restored after a skipped (retrying) cycle.
        self._nominal_interval = timedelta(hours=interval_hours)

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(hours=interval_hours),
        )

    # ── Config helpers ────────────────────────────────────────────────────────

    @property
    def _merged_config(self) -> dict[str, Any]:
        """Merge entry.data with entry.options (options take precedence)."""
        return {**self._entry.data, **self._entry.options}

    def _cache_dir(self, source: str) -> str:
        return self.hass.config.path(DOMAIN, "cache", source)

    def _index_file(self) -> str:
        return self.hass.config.path(DOMAIN, "cache", "theframetv_index.json")

    def _token_file(self) -> str:
        return self.hass.config.path(".storage", f"{DOMAIN}_{self._entry.entry_id}_tv_token")

    # ── DataUpdateCoordinator callback ────────────────────────────────────────

    async def _async_update_data(self) -> dict[str, Any]:
        """Called by HA on the rotation interval. Skips artwork change on first call."""
        if self._first_refresh:
            self._first_refresh = False
            stored = await self._store.async_load() or {}
            return stored.get("current", {})

        return await self._do_update(force=False)

    # ── Public API ────────────────────────────────────────────────────────────

    async def async_force_update(self) -> None:
        """Push a new artwork right now, even if the TV is in use.

        Bound to the button entity: pressing it is an explicit request, so the
        "do not interrupt" protection is deliberately bypassed.
        """
        await self._async_manual_update(force=True)

    async def async_request_update(self) -> None:
        """Push a new artwork now, unless the TV is currently being watched.

        Bound to the update_artwork service, which is typically called from
        automations: there the protection still applies.
        """
        await self._async_manual_update(force=False)

    async def _async_manual_update(self, force: bool) -> None:
        try:
            result = await self._do_update(force=force)
            self.async_set_updated_data(result)
        except UpdateFailed as err:
            _LOGGER.error("Forced artwork update failed: %s", err)
            raise

    async def async_refresh_theframetv_index(self) -> None:
        """Re-scrape theframetv.com and persist the new index."""
        index_file = self._index_file()
        _LOGGER.info("Refreshing theframetv.com artwork index...")
        await self.hass.async_add_executor_job(theframetv.refresh_index, index_file)
        _LOGGER.info("theframetv.com index refreshed")

    # ── Internal update logic ─────────────────────────────────────────────────

    async def _do_update(self, force: bool = False) -> dict[str, Any]:
        """Pick a source → fetch image (executor) → upload to TV (async).

        When `force` is False and the TV is currently showing content, nothing
        is downloaded or uploaded: the cycle is skipped and retried shortly.
        """
        cfg = self._merged_config

        # Checked before any download: pushing an artwork switches the TV into
        # Art Mode, which would interrupt whatever is being watched.
        if not force and await self._async_should_skip(cfg):
            stored = await self._store.async_load() or {}
            return stored.get("current", self.data or {})

        self._restore_nominal_interval()

        # Load persistent history
        stored = await self._store.async_load() or {}
        history_size = cfg.get(CONF_HISTORY_SIZE, DEFAULT_HISTORY_SIZE)
        tf_history: list[str] = stored.get("theframetv_history", [])[-history_size:]
        ic_history: list[str] = stored.get("icloud_history", [])[-history_size:]

        source = self._pick_source(cfg)
        if source is None:
            raise UpdateFailed("No artwork source is enabled")

        # Pre-compute paths (safe to access from main thread)
        index_file = self._index_file()
        tf_cache = self._cache_dir("theframetv")
        ic_cache = self._cache_dir("icloud")

        # Keep the theframetv catalogue fresh, otherwise the rotation keeps
        # cycling over the same stale (and possibly seasonal) artworks.
        if cfg.get(CONF_THEFRAMETV_ENABLED, True):
            await self._async_refresh_index_if_stale(cfg, index_file)

        # Fetch image in executor (sync network calls)
        result = await self.hass.async_add_executor_job(
            self._get_image_sync,
            cfg, source, tf_history, ic_history, index_file, tf_cache, ic_cache,
        )

        # Fallback to the other source if primary fails
        fallback_source = source
        if result is None:
            fallback = "icloud" if source == "theframetv" else "theframetv"
            enabled_key = CONF_ICLOUD_ENABLED if fallback == "icloud" else CONF_THEFRAMETV_ENABLED
            if cfg.get(enabled_key, False):
                _LOGGER.warning("Primary source %s failed, trying fallback %s", source, fallback)
                result = await self.hass.async_add_executor_job(
                    self._get_image_sync,
                    cfg, fallback, tf_history, ic_history, index_file, tf_cache, ic_cache,
                )
                fallback_source = fallback
                if result is not None:
                    source = fallback

        if result is None:
            tried = [source] if source == fallback_source else [source, fallback_source]
            raise UpdateFailed(
                "No image could be obtained from the enabled source(s): "
                + ", ".join(t for t in tried if t)
                + ". Check the Home Assistant log for the per-source reason "
                  "(invalid iCloud link, empty album, or theframetv.com unreachable)."
            )

        # Upload to the TV (native async)
        success = await uploader.upload_to_frame(
            self.hass,
            image_path=result["path"],
            tv_ip=cfg[CONF_TV_IP],
            tv_port=cfg.get(CONF_TV_PORT, DEFAULT_TV_PORT),
            token_file=self._token_file(),
            image_config={
                "width": cfg.get(CONF_IMAGE_WIDTH, DEFAULT_IMAGE_WIDTH),
                "height": cfg.get(CONF_IMAGE_HEIGHT, DEFAULT_IMAGE_HEIGHT),
                "mode": cfg.get(CONF_IMAGE_MODE, DEFAULT_IMAGE_MODE),
                "jpeg_quality": 95,
            },
            max_tv_images=cfg.get(CONF_MAX_TV_IMAGES, DEFAULT_MAX_TV_IMAGES),
        )
        if not success:
            raise UpdateFailed(
                f"Failed to upload image to TV at {cfg[CONF_TV_IP]} – the TV internal "
                "memory may be full, or the TV may be unreachable / not in Art Mode"
            )

        # Persist history and current state
        if source == "theframetv":
            tf_history.append(result["source_id"])
        else:
            ic_history.append(result["source_id"])

        current = {
            "name": self._clean_name(result["name"]),
            "source": source,
            "path": result["path"],
            "last_update": dt_util.now().isoformat(),
        }
        await self._store.async_save({
            "theframetv_history": tf_history,
            "icloud_history": ic_history,
            "current": current,
        })

        # Best-effort disk housekeeping, never fatal for the cycle.
        await self._async_purge_caches(cfg, keep_path=result["path"])

        return current

    async def _async_should_skip(self, cfg: dict[str, Any]) -> bool:
        """True when the TV is in use and the rotation must not interrupt it."""
        if not cfg.get(CONF_SKIP_WHEN_WATCHING, DEFAULT_SKIP_WHEN_WATCHING):
            return False

        state = await uploader.get_art_state(
            self.hass,
            tv_ip=cfg[CONF_TV_IP],
            tv_port=cfg.get(CONF_TV_PORT, DEFAULT_TV_PORT),
            token_file=self._token_file(),
        )

        if state == ART_STATE_BUSY:
            retry = timedelta(minutes=RETRY_WHEN_BUSY_MINUTES)
            _LOGGER.info(
                "TV is in use (Art Mode off) – skipping this rotation, retrying in %d min",
                RETRY_WHEN_BUSY_MINUTES,
            )
            if self.update_interval != retry:
                self.update_interval = retry
            return True

        if state == ART_STATE_UNREACHABLE:
            # Undetermined state: carry on, the upload itself reports a clear error.
            _LOGGER.debug("Art Mode state undetermined, proceeding with the rotation")
        return False

    def _restore_nominal_interval(self) -> None:
        """Return to the configured rotation interval after a retry cycle."""
        if self.update_interval != self._nominal_interval:
            _LOGGER.debug("Restoring the nominal rotation interval")
            self.update_interval = self._nominal_interval

    async def _async_refresh_index_if_stale(self, cfg: dict[str, Any], index_file: str) -> None:
        """Refresh the theframetv index when it is missing or older than the configured age."""
        max_age_days = cfg.get(CONF_INDEX_REFRESH_DAYS, DEFAULT_INDEX_REFRESH_DAYS)
        try:
            age_days = await self.hass.async_add_executor_job(
                theframetv.index_age_days, index_file
            )
        except OSError as err:
            _LOGGER.debug("Could not determine theframetv index age: %s", err)
            age_days = None

        if age_days is not None and age_days < max_age_days:
            return

        _LOGGER.info(
            "theframetv index is %s – refreshing (max age %s days)",
            "missing" if age_days is None else f"{age_days:.1f} days old",
            max_age_days,
        )
        try:
            artworks = await self.hass.async_add_executor_job(
                theframetv.refresh_index, index_file
            )
        except Exception as err:  # noqa: BLE001 – scraping can fail in many ways
            _LOGGER.warning(
                "theframetv index refresh failed (%s); continuing with the existing index", err
            )
            return
        _LOGGER.info("theframetv index refreshed: %d artworks", len(artworks or []))

    async def _async_purge_caches(self, cfg: dict[str, Any], keep_path: str | None) -> None:
        """Trim each source cache directory back under the configured size budget."""
        max_mb = cfg.get(CONF_CACHE_MAX_MB, DEFAULT_CACHE_MAX_MB)
        try:
            max_bytes = int(max_mb) * 1024 * 1024
        except (TypeError, ValueError):
            max_bytes = DEFAULT_CACHE_MAX_MB * 1024 * 1024
        if max_bytes <= 0:
            return

        cache_dirs = [self._cache_dir("theframetv"), self._cache_dir("icloud")]
        index_file = self._index_file()
        try:
            await self.hass.async_add_executor_job(
                self._purge_caches_sync, cache_dirs, max_bytes, keep_path, index_file
            )
        except Exception as err:  # noqa: BLE001 – housekeeping must never break a cycle
            _LOGGER.warning("Cache purge failed: %s", err)

    # ── Sync helpers (run in executor) ────────────────────────────────────────

    @staticmethod
    def _clean_name(name: Any) -> str:
        """Strip the scraped SEO prefix and clamp the title to a valid HA state length."""
        text = str(name or "").strip()
        if not text:
            return "unknown"

        stripped = _NAME_PREFIX_RE.sub("", text, count=1).strip()
        # Collapse separators left over from slug-like titles.
        stripped = re.sub(r"\s+", " ", stripped).strip(" -_:|")
        if not stripped:
            stripped = text

        if len(stripped) > MAX_NAME_LENGTH:
            stripped = stripped[:MAX_NAME_LENGTH].rstrip()
        return stripped

    @staticmethod
    def _purge_caches_sync(
        cache_dirs: list[str],
        max_bytes: int,
        keep_path: str | None,
        index_file: str,
    ) -> int:
        """LRU-purge regular files inside the given cache directories. Best effort.

        Scope is deliberately narrow: only direct children of the given directories,
        only regular files, no recursion, no directory removal.
        """
        keep_real = os.path.realpath(keep_path) if keep_path else None
        index_real = os.path.realpath(index_file) if index_file else None
        removed = 0

        for cache_dir in cache_dirs:
            cache_real = os.path.realpath(cache_dir)
            entries: list[tuple[float, int, str]] = []
            total = 0
            try:
                with os.scandir(cache_real) as it:
                    for entry in it:
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        try:
                            st = entry.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        total += st.st_size
                        # Prefer access time; fall back to mtime when atime is unusable.
                        last_used = st.st_atime or st.st_mtime
                        entries.append((last_used, st.st_size, entry.path))
            except FileNotFoundError:
                continue
            except OSError as err:
                _LOGGER.warning("Could not scan cache directory %s: %s", cache_real, err)
                continue

            if total <= max_bytes:
                continue

            _LOGGER.info(
                "Cache %s is %.1f MB (limit %.1f MB) – purging least recently used files",
                cache_real, total / 1048576, max_bytes / 1048576,
            )

            for _last_used, size, path in sorted(entries, key=lambda item: item[0]):
                if total <= max_bytes:
                    break
                real = os.path.realpath(path)
                # Hard scope guards: stay strictly inside this cache directory.
                if os.path.dirname(real) != cache_real:
                    continue
                if real == keep_real or real == index_real:
                    continue
                if real.lower().endswith(".json"):
                    continue
                if not os.path.isfile(real) or os.path.islink(path):
                    continue
                try:
                    os.remove(path)
                except OSError as err:
                    _LOGGER.debug("Could not remove cached file %s: %s", path, err)
                    continue
                total -= size
                removed += 1

        if removed:
            _LOGGER.info("Cache purge removed %d file(s)", removed)
        return removed

    @staticmethod
    def _pick_source(cfg: dict[str, Any]) -> str | None:
        candidates: list[str] = []
        weights: list[int] = []
        if cfg.get(CONF_THEFRAMETV_ENABLED, True):
            candidates.append("theframetv")
            weights.append(cfg.get(CONF_THEFRAMETV_WEIGHT, DEFAULT_THEFRAMETV_WEIGHT))
        if cfg.get(CONF_ICLOUD_ENABLED, False):
            candidates.append("icloud")
            weights.append(cfg.get(CONF_ICLOUD_WEIGHT, DEFAULT_ICLOUD_WEIGHT))
        if not candidates:
            return None
        return random.choices(candidates, weights=weights, k=1)[0]

    @staticmethod
    def _get_image_sync(
        cfg: dict[str, Any],
        source: str,
        tf_history: list[str],
        ic_history: list[str],
        index_file: str,
        tf_cache: str,
        ic_cache: str,
    ) -> dict[str, Any] | None:
        """Fetch and download an image from the chosen source. Runs in executor."""
        if source == "theframetv":
            artworks = theframetv.load_index(index_file)
            if not artworks:
                artworks = theframetv.refresh_index(index_file)
            if not artworks:
                return None

            avoid = set(tf_history)
            candidates = [a for a in artworks if a["page_url"] not in avoid]
            if not candidates:
                candidates = artworks  # full cycle: restart from scratch

            artwork = random.choice(candidates)
            path = theframetv.download_image(artwork, tf_cache)
            if not path:
                return None
            return {
                "path": path,
                "name": artwork["name"],
                "source_id": artwork["page_url"],
            }

        if source == "icloud":
            share_url = cfg.get(CONF_ICLOUD_URL, "").strip()
            if not share_url:
                return None

            photos = icloud.fetch_photo_list(share_url)
            if not photos:
                return None

            avoid = set(ic_history)
            candidates = [p for p in photos if p.get("filename") not in avoid]
            if not candidates:
                candidates = photos

            photo = random.choice(candidates)
            path = icloud.download_image(photo, ic_cache)
            if not path:
                return None
            return {
                "path": path,
                "name": photo.get("filename", "unknown"),
                "source_id": photo.get("filename", ""),
            }

        return None
