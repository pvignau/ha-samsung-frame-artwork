"""Coordinator for Samsung Frame – manages artwork rotation."""
from __future__ import annotations

import os
import random
import logging
from datetime import timedelta, datetime
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.helpers.storage import Store

from .const import (
    DOMAIN, STORAGE_VERSION,
    CONF_TV_IP, CONF_TV_PORT,
    CONF_THEFRAMETV_ENABLED, CONF_THEFRAMETV_WEIGHT,
    CONF_ICLOUD_ENABLED, CONF_ICLOUD_WEIGHT, CONF_ICLOUD_URL,
    CONF_INTERVAL_HOURS, CONF_HISTORY_SIZE,
    CONF_IMAGE_MODE, CONF_IMAGE_WIDTH, CONF_IMAGE_HEIGHT,
    DEFAULT_TV_PORT, DEFAULT_THEFRAMETV_WEIGHT, DEFAULT_ICLOUD_WEIGHT,
    DEFAULT_INTERVAL_HOURS, DEFAULT_HISTORY_SIZE,
    DEFAULT_IMAGE_MODE, DEFAULT_IMAGE_WIDTH, DEFAULT_IMAGE_HEIGHT,
)
from .sources import theframetv, icloud
from . import uploader

_LOGGER = logging.getLogger(__name__)


class SamsungFrameCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetches a new artwork and pushes it to the Frame TV on the configured schedule."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._entry = entry
        self._store: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}_{entry.entry_id}")
        # Avoid uploading artwork on the very first HA startup refresh
        self._first_refresh = True

        cfg = self._merged_config
        interval_hours = cfg.get(CONF_INTERVAL_HOURS, DEFAULT_INTERVAL_HOURS)

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

        return await self._do_update()

    # ── Public API ────────────────────────────────────────────────────────────

    async def async_force_update(self) -> None:
        """Force an immediate artwork update (called by button entity or service)."""
        try:
            result = await self._do_update()
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

    async def _do_update(self) -> dict[str, Any]:
        """Pick a source → fetch image (executor) → upload to TV (async)."""
        cfg = self._merged_config

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

        # Fetch image in executor (sync network calls)
        result = await self.hass.async_add_executor_job(
            self._get_image_sync,
            cfg, source, tf_history, ic_history, index_file, tf_cache, ic_cache,
        )

        # Fallback to the other source if primary fails
        if result is None:
            fallback = "icloud" if source == "theframetv" else "theframetv"
            enabled_key = CONF_ICLOUD_ENABLED if fallback == "icloud" else CONF_THEFRAMETV_ENABLED
            if cfg.get(enabled_key, False):
                _LOGGER.warning("Primary source %s failed, trying fallback %s", source, fallback)
                result = await self.hass.async_add_executor_job(
                    self._get_image_sync,
                    cfg, fallback, tf_history, ic_history, index_file, tf_cache, ic_cache,
                )

        if result is None:
            raise UpdateFailed("All artwork sources failed to produce an image")

        # Upload to the TV (native async)
        success = await uploader.upload_to_frame(
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
        )
        if not success:
            raise UpdateFailed(f"Failed to upload image to TV at {cfg[CONF_TV_IP]}")

        # Persist history and current state
        if source == "theframetv":
            tf_history.append(result["source_id"])
        else:
            ic_history.append(result["source_id"])

        current = {
            "name": result["name"],
            "source": source,
            "path": result["path"],
            "last_update": datetime.now().isoformat(),
        }
        await self._store.async_save({
            "theframetv_history": tf_history,
            "icloud_history": ic_history,
            "current": current,
        })

        return current

    # ── Sync helpers (run in executor) ────────────────────────────────────────

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
