# Voltahub Bridge

Edge agent for the [Voltahub](https://voltahub.eu) platform. Runs on a Raspberry Pi at the customer's home and collects local energy data.

## Installation

On a fresh Raspberry Pi OS Lite install, a single bare command is enough — no arguments needed:

```bash
curl -fsSL https://raw.githubusercontent.com/voltahub-eu/bridge/main/install.sh \
  | sudo bash
```

This installs the agent and starts the service immediately — no separate onboarding step, no arguments to pass. The device is reachable at `http://voltahub.local:8080`. You link the token and plugin(s) afterwards, entirely via the local status page and the platform:
- **Token:** via the "Agent token" field on the local status page — automatically restarts the service with the new value.
- **Plugin(s):** via the platform (config push to the device once it's linked).

**Update:** `scripts/update.sh` is triggered remotely by the platform (via `core/sync.py`) and checks out the given git tag, with a sanity check and automatic fallback to the previous version on a failed update.

## Structure

```
bridge/
├── install.sh                    # One-command installation, all arguments optional
├── main.py                       # Entrypoint: bootstrap agent + local web server
├── config.py                     # Config (env variables)
├── requirements.txt
├── core/
│   ├── agent.py                  # Bootstrap, plugin lifecycle, config push, commands
│   ├── database.py                # SQLite: device config, plugins, readings
│   ├── env_file.py                # Writes AGENT_KEY to /etc/voltahub-bridge/env
│   ├── plugin.py                  # DevicePlugin/Reading/Command base classes
│   ├── plugin_download.py         # OTA download of individual plugins (GitHub)
│   ├── supervisor.py              # Start/stop/restart of plugin tasks
│   ├── sync.py                    # WebSocket connection to the platform
│   └── health.py                  # Per-plugin status (for the status page)
├── plugins/                       # Vendored plugins (HomeWizard, SolarEdge, Enphase, ...)
├── web/
│   ├── server.py                  # Local FastAPI status page + /api/token
│   └── static/                    # Status page UI (see screenshot in the admin panel)
├── systemd/
│   └── voltahub-bridge.service    # The only service — no separate onboarding/portal service anymore
└── scripts/
    └── update.sh                  # OTA core update, triggered remotely by the platform
```

## Plugins

Plugins are loaded dynamically based on the config the platform pushes. On a new/changed plugin version, the Pi automatically downloads the required files from GitHub.

Each plugin inherits from `DevicePlugin` (`core/plugin.py`) and implements `poll()`.

## Management

```bash
sudo systemctl status voltahub-bridge
sudo journalctl -u voltahub-bridge -f
```
