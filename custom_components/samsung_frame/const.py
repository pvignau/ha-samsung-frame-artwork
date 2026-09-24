"""Constants for the Samsung Frame Artwork integration."""

DOMAIN = "samsung_frame"
PLATFORMS = ["sensor", "button"]

# Config keys (stored in entry.data / entry.options)
CONF_TV_IP = "tv_ip"
CONF_TV_PORT = "tv_port"
CONF_THEFRAMETV_ENABLED = "theframetv_enabled"
CONF_THEFRAMETV_WEIGHT = "theframetv_weight"
CONF_ICLOUD_ENABLED = "icloud_enabled"
CONF_ICLOUD_WEIGHT = "icloud_weight"
CONF_ICLOUD_URL = "icloud_share_url"
CONF_INTERVAL_HOURS = "interval_hours"
CONF_HISTORY_SIZE = "history_size"
CONF_IMAGE_MODE = "image_mode"
CONF_IMAGE_WIDTH = "image_width"
CONF_IMAGE_HEIGHT = "image_height"
CONF_MAX_TV_IMAGES = "max_tv_images"
CONF_CACHE_MAX_MB = "cache_max_mb"
CONF_INDEX_REFRESH_DAYS = "index_refresh_days"
CONF_SKIP_WHEN_WATCHING = "skip_when_watching"

# Default values
DEFAULT_TV_PORT = 8002
DEFAULT_THEFRAMETV_WEIGHT = 50
DEFAULT_ICLOUD_WEIGHT = 50
DEFAULT_INTERVAL_HOURS = 6
DEFAULT_HISTORY_SIZE = 20
DEFAULT_IMAGE_MODE = "fill"
# fill  : centre-crop to the screen ratio
# fit   : whole image, letterboxed
# smart : crop anchored on detected faces, centred when nobody is found
IMAGE_MODES = ("fill", "fit", "smart")
DEFAULT_IMAGE_WIDTH = 3840
DEFAULT_IMAGE_HEIGHT = 2160
DEFAULT_MAX_TV_IMAGES = 10
DEFAULT_CACHE_MAX_MB = 300
DEFAULT_INDEX_REFRESH_DAYS = 7
DEFAULT_SKIP_WHEN_WATCHING = True

# When a rotation is skipped because the TV is in use, retry this often instead
# of waiting for the next full interval – otherwise switching the TV off in the
# evening would leave the same artwork until the middle of the night.
RETRY_WHEN_BUSY_MINUTES = 15

# Values returned by uploader.get_art_state()
ART_STATE_ART = "art"                  # TV is showing Art Mode: safe to push
ART_STATE_BUSY = "busy"                # TV is on and showing content: do not interrupt
ART_STATE_UNREACHABLE = "unreachable"  # state could not be determined

# Samsung Art Mode category holding user-uploaded images
UPLOAD_CATEGORY = "MY-C0002"

# Bounds for image dimensions accepted by the config flow
MIN_IMAGE_DIMENSION = 640
MAX_IMAGE_DIMENSION = 7680

# Storage
STORAGE_VERSION = 1
