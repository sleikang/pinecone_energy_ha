"""Constants for the 松果能源 integration."""

DOMAIN = "pinecone_energy"
DEFAULT_SCAN_INTERVAL = 30
MIN_SCAN_INTERVAL = 10
MAX_SCAN_INTERVAL = 300

CONF_PHONE = "phone"
CONF_TOKEN = "token"
CONF_PILES = "piles"
CONF_NICKNAME = "nickname"

# Period sensors keep their own rolling history; this is how far back it goes.
CONF_HISTORY_DAYS = "history_days"
DEFAULT_HISTORY_DAYS = 730
MIN_HISTORY_DAYS = 30
MAX_HISTORY_DAYS = 3650
