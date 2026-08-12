#!/bin/bash
# OTA update of the agent core. Is NOT invoked directly from the working
# tree — core/sync.py::_trigger_update() first copies this file to /tmp and
# runs that copy. Reason: this script itself does a `git checkout` in
# /opt/voltahub-bridge, and if it kept running in-place from that working
# tree, bash could switch halfway to the new (not yet fully loaded) script
# content — executing a mix of old/new code. Running from /tmp, this script
# is no longer part of what git just overwrote.
#
# Order is deliberate: never stop the service before this script is done
# (otherwise that would kill the update halfway) — the systemd restart is
# always the very last step.
set -uo pipefail

INSTALL_DIR="/opt/voltahub-bridge"
TARGET_VERSION="${1:?usage: update.sh <git-tag>}"

cd "$INSTALL_DIR" || exit 1
PREV_COMMIT=$(git rev-parse HEAD)

echo "=== Voltahub Bridge OTA: updating to $TARGET_VERSION (current: $PREV_COMMIT) ==="

report_failure() {
  local error_msg="$1"
  echo "FOUT: $error_msg" >&2
  curl -s -X POST "${PLATFORM_API_URL:-https://api.voltahub.eu}/agent/update-result" \
    -H "X-Api-Key: ${AGENT_KEY:-}" -H "Content-Type: application/json" \
    -d "{\"success\": false, \"version\": \"$TARGET_VERSION\", \"error\": \"$error_msg\"}" \
    >/dev/null 2>&1 || true
}

if ! git fetch --tags 2>&1; then
  report_failure "git fetch failed"
  exit 1
fi

# -f: ignore local changes in tracked files (e.g. a chmod +x that a previous
# install.sh/update once left behind) — this is a deployment working tree,
# not a place for manual/local edits, so they may always be overwritten by
# the release we just fetched.
if ! git checkout -f "$TARGET_VERSION" 2>&1; then
  report_failure "git checkout to $TARGET_VERSION failed"
  git checkout -f "$PREV_COMMIT" 2>&1
  exit 1
fi

if ! "$INSTALL_DIR/venv/bin/pip" install -q -r requirements.txt 2>&1; then
  report_failure "pip install failed after checkout to $TARGET_VERSION"
  git checkout -f "$PREV_COMMIT" 2>&1
  systemctl restart voltahub-bridge
  exit 1
fi

# Sanity check: never let a broken release become active. Deliberately broad
# (all .py files) — a syntax error anywhere would immediately crash the
# agent after restart.
if ! "$INSTALL_DIR/venv/bin/python" -c "
import py_compile, pathlib, sys
failed = False
for f in pathlib.Path('.').rglob('*.py'):
    if 'venv' in f.parts:
        continue
    try:
        py_compile.compile(str(f), doraise=True)
    except py_compile.PyCompileError as e:
        print(e, file=sys.stderr)
        failed = True
sys.exit(1 if failed else 0)
"; then
  report_failure "py_compile sanity check failed for $TARGET_VERSION, rolled back to $PREV_COMMIT"
  git checkout -f "$PREV_COMMIT" 2>&1
  systemctl restart voltahub-bridge
  exit 1
fi

echo "Sanity check OK, restarting agent..."
systemctl restart voltahub-bridge
# Success is not reported here (this process/the restarting service doesn't
# survive this cleanly anyway) — the heartbeat after restart with the new
# agent_version confirms success to the platform (see _apply_heartbeat).
