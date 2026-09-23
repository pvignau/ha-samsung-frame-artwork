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

# Default values
DEFAULT_TV_PORT = 8002
DEFAULT_THEFRAMETV_WEIGHT = 50
DEFAULT_ICLOUD_WEIGHT = 50
DEFAULT_INTERVAL_HOURS = 6
DEFAULT_HISTORY_SIZE = 20
DEFAULT_IMAGE_MODE = "fill"
DEFAULT_IMAGE_WIDTH = 3840
DEFAULT_IMAGE_HEIGHT = 2160

# Storage
STORAGE_VERSION = 1
