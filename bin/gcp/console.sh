#!/usr/bin/env bash
# From the Mac: open the factory console, and Archon's own run view, through the main
# node. Nothing is exposed on the network: one IAP SSH connection forwards
#   localhost:8080 → /run/factory-console/sock   (the console; it sees who you are)
#   localhost:3090 → 127.0.0.1:3090               (Archon's web console)
#   bin/gcp/console.sh            # env: CONSOLE_PORT=8080 ARCHON_PORT=3090
set -euo pipefail
source "$(dirname "$0")/env.sh"
PROJECT=${PROJECT:-me-sb-dgcp-dpoc-pocyosh-pr}
ZONE=${ZONE:-me-west1-a}
VM=${MAIN_VM:-factory-1}
PORT=${CONSOLE_PORT:-8080}
ARCHON_PORT=${ARCHON_PORT:-3090}
F=(--project "$PROJECT" --zone "$ZONE" --tunnel-through-iap --quiet)

for p in "$PORT" "$ARCHON_PORT"; do
  if lsof -nP -iTCP:"$p" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "port $p is in use on this Mac; set CONSOLE_PORT / ARCHON_PORT" >&2; exit 1
  fi
done

echo "==> console operator on $VM"
# OS Login admins have sudo there; membership is what the console checks (SO_PEERCRED).
gcloud compute ssh "$VM" "${F[@]}" --command \
  'sudo gpasswd -a "$(id -un)" factory-operators >/dev/null && echo "    $(id -un) is in factory-operators"' 2>/dev/null

echo "==> console  http://localhost:$PORT/"
echo "    Archon   http://localhost:$ARCHON_PORT/        (Ctrl-C closes both)"
( sleep 4; command -v open >/dev/null && open "http://localhost:$PORT/" ) &
exec gcloud compute ssh "$VM" "${F[@]}" -- -N -o ExitOnForwardFailure=yes \
  -L "$PORT:/run/factory-console/sock" -L "$ARCHON_PORT:127.0.0.1:3090"
