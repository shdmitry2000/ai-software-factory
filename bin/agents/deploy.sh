#!/usr/bin/env bash
# ON THE MAIN NODE (factory-1): build the agent image here and apply a deployment from
# deploy/ (components, hosts, deployments) on this machine. From the Mac use
# remote-deploy.sh.
#   sudo bash /root/factory-deploy/bin/agents/deploy.sh [compliance@local]
# Google access comes from the VM's service account through the metadata server; the
# Claude login comes from /root/.factory-env (bin/gcp/push-logins.sh) as a mounted file.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd -P)
META=http://metadata.google.internal/computeMetadata/v1
md() { curl -fsS -H 'Metadata-Flavor: Google' "$META/$1"; }
md instance/name >/dev/null 2>&1 || { echo "run this on the main node (factory-1), not here" >&2; exit 1; }

PROJECT=${PROJECT:-$(md project/project-id)}
DEPLOYMENT=${1:-${DEPLOYMENT:-compliance@local}}
TAG=$(git -C "$ROOT" rev-parse --short=12 HEAD 2>/dev/null || date +%Y%m%d%H%M)
IMAGE=factory/agent:$TAG
UV=$(command -v uv || echo /root/.local/bin/uv)
rt() { PYTHONPATH="$ROOT/mission-control" "$UV" run -q --project "$ROOT/mission-control" python -m mission_control.runtime "$@"; }

echo "==> check deploy/"
rt spec

echo "==> docker"
if ! command -v docker >/dev/null; then          # the rebuilt VM has none; Ubuntu's package is enough
  DEBIAN_FRONTEND=noninteractive apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker.io >/dev/null
  systemctl enable --now docker >/dev/null
fi
docker version --format '    server {{.Server.Version}}'

echo "==> image $IMAGE"
docker build -q -f "$ROOT/agents/image/Dockerfile" -t "$IMAGE" "$ROOT" >/dev/null
ID=$(docker image inspect --format '{{.Id}}' "$IMAGE")
echo "    $ID"

echo "==> deploy $DEPLOYMENT"
rt apply-spec "$DEPLOYMENT" --digest "$ID" --var "PROJECT=$PROJECT"

echo "==> status"
rt status
sleep 20
echo "==> last log lines"
rt logs "$DEPLOYMENT" --tail 20

# keep the last 5 agent images, drop older ones
docker image ls factory/agent --format '{{.Tag}}' | tail -n +6 | xargs -r -I{} docker image rm -f "factory/agent:{}" >/dev/null 2>&1 || true
