#!/usr/bin/env bash
# ON THE MAIN NODE (factory-1) only: build the agent image, push it, deploy the
# compliance agent. The cluster accepts kubectl from this machine alone.
#   sudo -i bash /root/factory-deploy/bin/gke/deploy.sh
# From the Mac, bin/gke/remote-deploy.sh ships the repo here and runs this.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd -P)
META=http://metadata.google.internal/computeMetadata/v1
md() { curl -fsS -H 'Metadata-Flavor: Google' "$META/$1"; }
md instance/name >/dev/null 2>&1 || { echo "run this on the main node (factory-1), not here" >&2; exit 1; }

PROJECT=${PROJECT:-$(md project/project-id)}
REGION=${REGION:-me-west1}
CLUSTER=${CLUSTER:-factory-agents}
VERTEX_REGION=${VERTEX_REGION:-global}
REPO=$REGION-docker.pkg.dev/$PROJECT/mission-control/agent
TAG=$(git -C "$ROOT" rev-parse --short=12 HEAD 2>/dev/null || date +%Y%m%d%H%M)

echo "==> tools"
install_kube_tools() {   # gcloud on the VM is a snap without the GKE auth plugin: use Google's apt repo
  local list=/etc/apt/sources.list.d/google-cloud-sdk.list key=/usr/share/keyrings/cloud.google.gpg
  if [ ! -f "$list" ]; then
    curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg | gpg --dearmor --yes -o "$key"
    echo "deb [signed-by=$key] https://packages.cloud.google.com/apt cloud-sdk main" > "$list"
  fi
  apt-get update -qq && apt-get install -y -qq kubectl google-cloud-cli-gke-gcloud-auth-plugin >/dev/null
}
[ -x /usr/bin/kubectl ] && [ -x /usr/bin/gke-gcloud-auth-plugin ] || install_kube_tools
export PATH=/usr/bin:$PATH USE_GKE_GCLOUD_AUTH_PLUGIN=True
gcloud auth configure-docker "$REGION-docker.pkg.dev" --quiet >/dev/null

echo "==> image $REPO:$TAG"
docker build -q -f "$ROOT/agents/image/Dockerfile" -t "$REPO:$TAG" "$ROOT"
docker push -q "$REPO:$TAG" >/dev/null
DIGEST=$(docker inspect --format='{{index .RepoDigests 0}}' "$REPO:$TAG")
echo "    $DIGEST"

echo "==> kubectl (private endpoint, from this node only)"
gcloud container clusters get-credentials "$CLUSTER" --region "$REGION" --project "$PROJECT" --internal-ip >/dev/null

echo "==> rules ConfigMap from prompts/compliance/rules (a rules change = rerun this, no rebuild)"
kubectl get ns agents >/dev/null 2>&1 || kubectl create ns agents >/dev/null
kubectl -n agents create configmap compliance-rules --from-file="$ROOT/prompts/compliance/rules" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null

echo "==> deploy"
sed -e "s|\${IMAGE}|$DIGEST|g" -e "s|\${PROJECT}|$PROJECT|g" -e "s|\${VERTEX_REGION}|$VERTEX_REGION|g" \
  "$ROOT/bin/gke/k8s/compliance.yaml" | kubectl apply -f -
kubectl -n agents rollout restart deploy/compliance-agent >/dev/null   # pick up a changed ConfigMap
kubectl -n agents rollout status deploy/compliance-agent --timeout=600s
kubectl -n agents get pods -o wide
