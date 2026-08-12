"""
Manages /etc/voltahub-bridge/env — the file systemd (via EnvironmentFile=)
reads AGENT_KEY/PLATFORM_WS_URL/etc. from. Separate from the git working tree
in /opt/voltahub-bridge, so it survives a `git pull` + restart. Written by
install.sh (first time) and web/server.py (later updates via the local
status page)."""
import os

ENV_FILE = "/etc/voltahub-bridge/env"


def write_env(values: dict[str, str]) -> None:
    """Writes/replaces the given keys in the env file, other lines are left
    untouched."""
    os.makedirs(os.path.dirname(ENV_FILE), exist_ok=True)
    lines = []
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE) as f:
            lines = [l for l in f if not any(l.startswith(f"{k}=") for k in values)]
    for k, v in values.items():
        lines.append(f"{k}={v}\n")
    with open(ENV_FILE, "w") as f:
        f.writelines(lines)
    os.chmod(ENV_FILE, 0o600)


def write_agent_key(agent_key: str) -> None:
    """Writes/replaces AGENT_KEY in the env file. The agent itself doesn't
    need to know device_id — the platform derives that server-side from this
    token on every request/WS connection (see auth.get_current_device)."""
    write_env({"AGENT_KEY": agent_key})
