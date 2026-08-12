"""
Downloads a specific plugin version from GitHub, decoupled from the agent
core itself. Each plugin has its own tag namespace
("plugin-{plugin_id}-{version}") so a plugin release never triggers an
agent-core release (and vice versa) — only Pis that actually use this
plugin download it, via the target_version/target_sha256 the platform
sends along in the config push (see app/routers/agent.py::
_build_config_payload, joined with plugin_releases).
"""
import hashlib
import logging
import os

import aiohttp

import config

log = logging.getLogger("plugin_download")

RAW_BASE = "https://raw.githubusercontent.com"


async def ensure_plugin_version(plugin_id: str, target_version: str, expected_sha256: str | None) -> bool:
    """Downloads plugins/{plugin_id}/plugin.py for tag 'plugin-{plugin_id}-
    {target_version}' into PLUGIN_DIR, provided the sha256 matches (prevents
    a corrupt/tampered download — no separate signing infra needed because
    the checksum already lives in the platform, the same trust chain as the
    rest of the config that comes from the platform).

    Returns True on success. On failure, the previously installed (or
    vendored) version simply keeps running — a failed download must never
    take down a working plugin."""
    tag = f"plugin-{plugin_id}-{target_version}"
    url = f"{RAW_BASE}/{config.PLUGIN_REPO}/{tag}/plugins/{plugin_id}/plugin.py"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                resp.raise_for_status()
                content = await resp.read()
    except Exception as e:
        log.error("could not download plugin %s version %s (%s): %s", plugin_id, target_version, url, e)
        return False

    if expected_sha256:
        actual = hashlib.sha256(content).hexdigest()
        if actual != expected_sha256:
            log.error("checksum mismatch for plugin %s version %s (expected %s, got %s) — download ignored",
                       plugin_id, target_version, expected_sha256, actual)
            return False

    plugin_dir = os.path.join(config.PLUGIN_DIR, plugin_id)
    os.makedirs(plugin_dir, exist_ok=True)
    with open(os.path.join(plugin_dir, "plugin.py"), "wb") as f:
        f.write(content)
    log.info("plugin %s updated to version %s (tag %s)", plugin_id, target_version, tag)
    return True
