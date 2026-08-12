import os

PLATFORM_WS_URL = os.getenv("PLATFORM_WS_URL", "wss://api.voltahub.eu/agent/ws")
PLATFORM_API_URL = os.getenv("PLATFORM_API_URL", "https://api.voltahub.eu")
AGENT_KEY = os.getenv("AGENT_KEY", "")
DB_PATH = os.getenv("DB_PATH", "/data/agent.db")
PLUGIN_DIR = os.getenv("PLUGIN_DIR", "/data/plugins")
PLUGIN_REPO = os.getenv("PLUGIN_REPO", "voltahub-eu/bridge")
WEB_PORT = int(os.getenv("WEB_PORT", "8080"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Internal timing constants
READINGS_FLUSH_INTERVAL_S = 30
HEALTH_FLUSH_INTERVAL_S = 60
WS_RECONNECT_DELAY_S = 10
RESTART_BACKOFF_S = (30, 60, 120)
MAX_RESTART_ATTEMPTS = 5
# Safety net alongside the event-driven config_update pushes: if such a signal
# never arrives for whatever reason (missed race, brief WS hiccup), the agent
# simply fetches the full config again periodically.
CONFIG_REFRESH_INTERVAL_S = 900
# Synced readings are only kept locally as a short buffer for the status page
# and for recovering from a platform outage — once synced and past this age
# they're deleted, otherwise readings/agent.db grows without bound forever.
READINGS_RETENTION_DAYS = 7
READINGS_CLEANUP_INTERVAL_S = 3600
