# Samsung Frame Artwork

[![Validation](https://github.com/pvignau/ha-samsung-frame-artwork/actions/workflows/validate.yml/badge.svg)](https://github.com/pvignau/ha-samsung-frame-artwork/actions/workflows/validate.yml)
[![hacs](https://img.shields.io/badge/HACS-custom-41BDF5.svg)](https://hacs.xyz)
[![license](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

Home Assistant integration that automatically rotates the artwork displayed in
Art Mode on a **Samsung The Frame** TV.

## How it works

On a configurable interval (6 h by default), the integration picks a source at
random (weights are adjustable), downloads an image, crops it to a 3840×2160
JPEG (≤ 1.9 MB, the TV limit) and sends it to the TV through
[`samsungtvws`](https://github.com/NickWaterton/samsung-tv-ws-api).

### Sources

| Source | Description |
|---|---|
| **theframetv.com** | Catalogue of free 4K artworks, scraped and cached locally |
| **iCloud shared album** | Photos from a public iCloud album (`https://photos.icloud.com/shared/album/…`) |

A persistent history prevents the same artworks from coming back until the
catalogue has been cycled through.

#### iCloud albums: two generations of API

Apple migrated shared albums to **CloudKit**. The integration handles both:

1. **CloudKit** (recent links) — `records/resolve` returns an anonymous access
   token, the partition and the album zone; `changes/zone` then enumerates the
   photos with their signed URLs.
2. **`sharedstreams`** (older albums) — kept as an automatic fallback.

When the original is a HEIC, which Pillow cannot read without a plugin, Apple's
own JPEG derivative is used instead.

## Entities and services

- `sensor.<tv>_current_artwork` — artwork currently displayed (attributes:
  source, date, local path)
- `button.<tv>_update_artwork_now` — push a new artwork right now
- Service `samsung_frame.update_artwork` — same, from an automation
- Service `samsung_frame.refresh_theframetv_index` — force a catalogue re-scrape

## Options

| Option | Default | Purpose |
|---|---|---|
| `interval_hours` | 6 | Rotation interval |
| `history_size` | 20 | Anti-repeat history |
| `skip_when_watching` | enabled | **Do not interrupt viewing** (see below) |
| `image_mode` | `fill` | `fill` = centre crop, `fit` = letterboxed, `smart` = crop on faces |
| `image_width` / `image_height` | 3840 × 2160 | Target resolution |
| `max_tv_images` | 10 | **Images kept in the TV memory** |
| `cache_max_mb` | 300 | Maximum size of the local disk cache |
| `index_refresh_days` | 7 | How often the catalogue is re-scraped |

### Face-aware cropping (`smart`)

A portrait photo cropped to 16:9 loses the top of the head when the window is
centred. In `smart` mode, faces are detected (OpenCV Haar cascades) and the crop
window is anchored on them, with padding and a slight upward offset matching how
a portrait is normally composed. When nobody is on the photo — the common case
for theframetv.com artworks — the result is identical to `fill`.

Detection runs on a downscaled copy of the image (800 px on the longest side),
inside the Home Assistant executor.

> **`smart` needs OpenCV, which cannot be installed on Home Assistant OS.**
> `opencv-python-headless` publishes no musllinux wheel, and the Home Assistant
> container is Alpine-based, so the install falls back to a source build that
> fails. The library is therefore *not* declared as a requirement: on an
> installation where it is unavailable, `smart` behaves exactly like `fill` and
> logs a warning. On a glibc-based install (Home Assistant Container on Debian,
> or Core in a virtualenv), `pip install "opencv-python-headless>=4.12,<5"` in
> the Home Assistant environment enables it. The 4.x branch is required:
> OpenCV 5 removed `CascadeClassifier` and no longer ships the cascades.

### Do not interrupt viewing

Pushing an artwork switches the TV into Art Mode, which cuts off whatever is
playing. When this option is enabled, the scheduled rotation first checks the
Art Mode state: if the TV is showing content, the cycle is skipped — nothing is
downloaded or uploaded — and retried every 15 minutes, so the artwork changes
shortly after the TV is switched off.

Art Mode is the criterion, not the power state: on a Frame, `PowerState` reads
`on` both in Art Mode and while content is playing.

The **Update Artwork Now** button always pushes an image (an explicit manual
action); the `samsung_frame.update_artwork` service honours the protection,
since it is mostly called from automations.

## Installation

### Through HACS (recommended)

HACS → ⋮ menu → **Custom repositories** → add
`https://github.com/pvignau/ha-samsung-frame-artwork` with category
**Integration**, then install **Samsung Frame Artwork** and restart Home
Assistant.

### Manually

Copy `custom_components/samsung_frame/` into the Home Assistant `config/`
folder, then restart.

### Configuration

**Settings → Devices & services → Add integration → Samsung Frame Artwork**.

The TV must be on (or in Art Mode) during the first pairing: it shows an
authorisation prompt to accept with the remote. The token is then stored in
`.storage/samsung_frame_<entry_id>_tv_token`.

> **Reserve the IP address of the TV in your DHCP server.** An address change
> breaks the integration silently: images keep being downloaded, but nothing is
> sent any more.

## Disclaimer

theframetv.com is scraped, and the iCloud shared album APIs (CloudKit as well as
`sharedstreams`) are undocumented: they were reconstructed by observing Apple's
web client. Both sources may stop working without notice. When that happens, the
Home Assistant log names the exact step that fails.

## Release notes

### 1.3.1

`opencv-python-headless` is no longer declared as a requirement: it has no
musllinux wheel, so on Home Assistant OS the install failed and the whole
integration would not load. Face detection is now strictly optional, and
`smart` degrades to a centred crop when OpenCV is absent.

### 1.3.0

**`smart`** crop mode: faces present on the photo are detected and the crop
window is anchored on them, instead of the centred crop that beheads portraits.
With no face detected, the result is identical to `fill`.

Support for **modern iCloud shared albums**, which Apple migrated from the
`sharedstreams` API to CloudKit; the former API remains an automatic fallback.
HEIC originals, unreadable by Pillow, go through the JPEG derivative.

**Viewing protection**: the scheduled rotation no longer switches the TV into
Art Mode while you are watching something, and retries every 15 minutes.

### 1.2.0

Fix for the underlying outage: the integration **kept stacking images in the TV
internal memory without ever deleting any**, until it filled up — every upload
then failed silently. Old images are now purged automatically
(`max_tv_images`).

Also: periodic catalogue refresh (it was frozen), LRU purge of the disk cache,
theframetv.com scraper fix, fix for the iCloud partition redirect handling and
for a path traversal through iCloud file names, bounded image dimensions,
`unique_id` on the config entry, `samsungtvws` dependency pinned to a commit,
French translations.

The catalogue was also capped at 5 scraped pages, i.e. the 75 most recent
artworks — all from the same seasonal batch (77 % Christmas). The bound is
raised to 40 pages (the loop stops by itself on the first empty page):
**333 artworks** indexed, 21 % Christmas.

### 1.1.0

Initial version: config flow, two sources, weighted rotation, persistent
history.
