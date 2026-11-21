#!/usr/bin/env bash
# On the factory VM, as root: serve Archon's web console on :3090 as a systemd service.
#   sudo -i bash archon-console.sh
#
# The factory's pinned Archon tree must stay byte-for-byte clean (consumer.py refuses to
# run otherwise), so the console runs from its own copy of the same commit, where the web
# UI is built. Both share ARCHON_HOME (~/.archon), so the console shows the factory's runs.
# Reach it from a laptop with bin/gcp/console.sh (forwards localhost:3090 over SSH;
# no firewall rule for 3090 exists).
set -euo pipefail
export PATH="$HOME/.bun/bin:$HOME/.local/bin:$PATH"

PIN_DIR=$(ls -dt "$HOME"/.cache/factory/archon/*/ 2>/dev/null | head -1)
[ -n "$PIN_DIR" ] || { echo "no pinned Archon under ~/.cache/factory/archon; run factory.py init first" >&2; exit 1; }
PIN=$(basename "$PIN_DIR")
CONSOLE=$HOME/archon-console

if [ "$(git -C "$CONSOLE" rev-parse HEAD 2>/dev/null)" != "$PIN" ]; then
  echo "==> copying Archon $PIN to $CONSOLE"
  rm -rf "$CONSOLE"
  cp -a "$PIN_DIR" "$CONSOLE"
fi

echo "==> building the web console"
(cd "$CONSOLE" && bun run build:web >/tmp/archon-build-web.log 2>&1) || { tail -20 /tmp/archon-build-web.log; exit 1; }

echo "==> systemd service archon-console"
cat > /etc/systemd/system/archon-console.service <<UNIT
[Unit]
Description=Archon web console (mission control C3)
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$CONSOLE
Environment=HOME=$HOME
Environment=ARCHON_HOME=$HOME/.archon
Environment=PATH=$PATH:/usr/local/bin:/usr/bin:/bin
ExecStart=$HOME/.bun/bin/bun $CONSOLE/packages/cli/src/cli.ts serve --port 3090
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now archon-console >/dev/null 2>&1
systemctl restart archon-console

for i in $(seq 1 30); do
  code=$(curl -s -o /dev/null -w '%{http_code}' localhost:3090/ || true)
  [ "$code" = "200" ] && break
  sleep 2
done
echo "==> http://localhost:3090/ answers $code"
git -C "$PIN_DIR" status --porcelain | wc -l | xargs echo "pinned tree changes (must be 0):"
