#!/usr/bin/env bash
# On an engine machine, as root: install the mission-control bridge and (with --service)
# run it under systemd. Idempotent; rerun to upgrade.
#   sudo -i bash install-on-vm.sh [--service]
# Env: MC_BRANCH (default main), MC_PROJECT, MC_ENGINE (default: short hostname), MC_PRODUCT.
set -euo pipefail
export PATH="$HOME/.bun/bin:$HOME/.local/bin:$PATH"
BRANCH=${MC_BRANCH:-main}
PROJECT=${MC_PROJECT:-me-sb-dgcp-dpoc-pocyosh-pr}
ENGINE=${MC_ENGINE:-$(hostname -s)}
PRODUCT=${MC_PRODUCT:-file-to-markdown-convertor}
SRC=$HOME/mission-control-src
VENV=$HOME/mission-control-venv
CONF=$HOME/.mission-control.yaml

echo "==> source ($BRANCH)"
if [ -d "$SRC/.git" ]; then
  git -C "$SRC" fetch -q origin "$BRANCH" && git -C "$SRC" checkout -q -B "$BRANCH" "origin/$BRANCH"
else
  git clone -q --branch "$BRANCH" https://github.com/shdmitry2000/ai-software-factory "$SRC"
fi
git -C "$SRC" log --oneline -1

echo "==> venv"
[ -x "$VENV/bin/python" ] || uv venv -q "$VENV"
uv pip install -q --python "$VENV/bin/python" -e "$SRC/mission-control[pubsub,firestore]"

PIN=$(basename "$(ls -dt "$HOME"/.cache/factory/archon/*/ 2>/dev/null | head -1)" 2>/dev/null || true)
echo "==> config $CONF (engine $ENGINE, product $PRODUCT, archon ${PIN:0:8})"
cat > "$CONF" <<YAML
ports:
  engine:
    adapter: archon
    db: $HOME/.archon/archon.db
    engine: $ENGINE
    product: $PRODUCT
    pin: "$PIN"
  bus:
    adapter: pubsub
    project: $PROJECT
  store:
    adapter: firestore
    project: $PROJECT
YAML

if [ "${1:-}" = "--service" ]; then
  echo "==> systemd service mission-control-bridge"
  cat > /etc/systemd/system/mission-control-bridge.service <<UNIT
[Unit]
Description=Mission control bridge: Archon events -> factory-events
After=network-online.target
Wants=network-online.target

[Service]
Environment=MISSION_CONTROL_CONFIG=$CONF
Environment=MC_SCHEMA_DIR=$SRC/mission-control/schema
Environment=GRPC_VERBOSITY=ERROR
ExecStart=$VENV/bin/python -m mission_control.bridge
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable --now mission-control-bridge >/dev/null 2>&1
  systemctl restart mission-control-bridge
  sleep 3
  systemctl is-active mission-control-bridge
fi
