"""
Agent version, derived from the git tag instead of a manually maintained
constant — prevents forgetting to bump it on a release. Deliberately
separate from plugin versions (see core/plugin_download.py), which have
their own tag namespace and release cycle.

--match deliberately restricts `git describe` to tags that look like a core
version ('v' + digit). Without this restriction, `git describe --tags` could
pick a plugin tag ("plugin-{id}-{version}") as the "closest tag" as soon as
it's more recent than (or on the same commit as) the last core release tag —
the agent would then report its own core version as e.g.
"plugin-homewizard_p1-2.0.0", which breaks the heartbeat reconciliation in
app/routers/agent.py (agent_version would then never match target_version
again)."""
import logging
import subprocess
from pathlib import Path

log = logging.getLogger("version")

_INSTALL_DIR = Path(__file__).resolve().parent.parent
_cached: str | None = None


def get_agent_version() -> str:
    global _cached
    if _cached is not None:
        return _cached
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--always", "--match", "v[0-9]*"],
            cwd=_INSTALL_DIR, capture_output=True, text=True, timeout=5,
        )
        _cached = result.stdout.strip() or "unknown"
    except Exception as e:
        log.warning("could not determine agent version via git: %s", e)
        _cached = "unknown"
    return _cached
