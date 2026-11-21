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
uv pip install -q --python "$VENV/bin/python" -e "$SRC/mission-control[pubsub,firestore,console]"

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
    app_dir: $HOME/$PRODUCT
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

  echo "==> docker (for the command handler's deploys)"
  if ! command -v docker >/dev/null; then
    DEBIAN_FRONTEND=noninteractive apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker.io >/dev/null
    systemctl enable --now docker >/dev/null
  fi

  echo "==> systemd service mission-control-handler"
  cat > /etc/systemd/system/mission-control-handler.service <<UNIT
[Unit]
Description=Mission control command handler: factory-commands -> deploys on this host
After=network-online.target docker.service
Wants=network-online.target
[Service]
RuntimeDirectory=mission-control-handler
RuntimeDirectoryMode=0700
Environment=MC_SCHEMA_DIR=$SRC/mission-control/schema
Environment=GRPC_VERBOSITY=ERROR
ExecStart=$VENV/bin/python -m mission_control.handler --project $PROJECT --engine $ENGINE serve --repo $SRC --app-dir $HOME/$PRODUCT
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable --now mission-control-handler >/dev/null 2>&1
  systemctl restart mission-control-handler
  sleep 3
  systemctl is-active mission-control-handler

  echo "==> systemd service factory-console (unix socket, no TCP port)"
  groupadd -f factory-operators      # who may use the console; bin/gcp/console.sh adds OS Login admins
  cat > /etc/systemd/system/factory-console.service <<UNIT
[Unit]
Description=Factory console: operator UI on /run/factory-console/sock (reach it with bin/gcp/console.sh)
After=network-online.target
Wants=network-online.target
[Service]
RuntimeDirectory=factory-console
RuntimeDirectoryMode=0755
Environment=MC_SCHEMA_DIR=$SRC/mission-control/schema
Environment=GRPC_VERBOSITY=ERROR
ExecStart=$VENV/bin/python -m mission_control.console --uds /run/factory-console/sock --project $PROJECT --engine $ENGINE
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable --now factory-console >/dev/null 2>&1
  systemctl restart factory-console
  sleep 3
  systemctl is-active factory-console
  curl -s --unix-socket /run/factory-console/sock http://console/api/health; echo
fi
