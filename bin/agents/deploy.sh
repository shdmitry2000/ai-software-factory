#!/usr/bin/env bash
# ON THE MAIN NODE (factory-1): build the agent image here and run the compliance agent
# as a Docker container on this machine (Stage R2). From the Mac use remote-deploy.sh.
#   sudo -i bash /root/factory-deploy/bin/agents/deploy.sh      # env: LLM=project|api_key|none
# Google access comes from the VM's service account through the metadata server; the
# Claude login comes from /root/.factory-env (bin/gcp/push-logins.sh) as a mounted file.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd -P)
META=http://metadata.google.internal/computeMetadata/v1
md() { curl -fsS -H 'Metadata-Flavor: Google' "$META/$1"; }
md instance/name >/dev/null 2>&1 || { echo "run this on the main node (factory-1), not here" >&2; exit 1; }

PROJECT=${PROJECT:-$(md project/project-id)}
PRODUCT=${PRODUCT:-file-to-markdown-convertor}
LLM=${LLM:-project}
TAG=$(git -C "$ROOT" rev-parse --short=12 HEAD 2>/dev/null || date +%Y%m%d%H%M)
IMAGE=factory/agent:$TAG
rt() { PYTHONPATH="$ROOT/mission-control" python3 -m mission_control.runtime "$@"; }

echo "==> image $IMAGE"
docker build -q -f "$ROOT/agents/image/Dockerfile" -t "$IMAGE" "$ROOT" >/dev/null
ID=$(docker image inspect --format '{{.Id}}' "$IMAGE")
echo "    $ID"

echo "==> deploy compliance@local (llm: $LLM)"
rt apply --component compliance --image "$IMAGE" --digest "$ID" --llm "$LLM" \
  --run-as 10001 --tmpfs /tmp --tmpfs /home/agent --memory 512m --cpus 0.5 \
  --env "GCP_PROJECT=$PROJECT" --env "FACTORY_PRODUCT=$PRODUCT" --env MC_TOPIC=factory-events \
  -- --bus pubsub --store firestore --interval 600

echo "==> status"
rt status
sleep 20
echo "==> last log lines"
rt logs compliance@local --tail 20

# keep the last 5 agent images, drop older ones
docker image ls factory/agent --format '{{.Tag}}' | tail -n +6 | xargs -r -I{} docker image rm -f "factory/agent:{}" >/dev/null 2>&1 || true
