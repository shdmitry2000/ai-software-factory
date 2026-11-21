#!/usr/bin/env bash
# Create (or bring back after the weekend sweep) the private agent cluster. Idempotent.
#   bin/gke/create-cluster.sh            # from the Mac (Google APIs only, no kubectl)
#
# Security (docs/mission-control-design.md#security-everything-through-the-main-node):
# - Autopilot, private nodes, private control-plane endpoint only; the one authorized
#   network is factory-1's internal IP; DNS endpoint off. kubectl works only on factory-1.
# - factory-vm (factory-1's service account) is the only identity granted cluster access.
# - Pods get Vertex AI, Pub/Sub publish and Firestore read through Workload Identity:
#   no keys, no Secrets. Egress to GitHub through Cloud NAT.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd -P)
source "$ROOT/bin/gcp/env.sh"

PROJECT=${PROJECT:-me-sb-dgcp-dpoc-pocyosh-pr}
REGION=${REGION:-me-west1}
ZONE=${ZONE:-me-west1-a}
CLUSTER=${CLUSTER:-factory-agents}
MAIN_VM=${MAIN_VM:-factory-1}
NETWORK=${NETWORK:-default}
TOPIC=${TOPIC:-factory-events}
VM_SA=factory-vm@$PROJECT.iam.gserviceaccount.com
NODE_SA_NAME=factory-gke-node
NODE_SA=$NODE_SA_NAME@$PROJECT.iam.gserviceaccount.com
g() { gcloud --project="$PROJECT" "$@"; }

echo "==> APIs"
g services enable container.googleapis.com aiplatform.googleapis.com artifactregistry.googleapis.com \
  pubsub.googleapis.com firestore.googleapis.com iam.googleapis.com >/dev/null

PROJECT_NUM=$(g projects describe "$PROJECT" --format='value(projectNumber)')
MAIN_IP=$(g compute instances describe "$MAIN_VM" --zone "$ZONE" --format='value(networkInterfaces[0].networkIP)')
[ -n "$MAIN_IP" ] || { echo "main node $MAIN_VM not found; create it first (bin/gcp/rebuild.sh)" >&2; exit 1; }
echo "    main node $MAIN_VM = $MAIN_IP"

echo "==> Private Google Access on $NETWORK/$REGION, Cloud NAT for GitHub"
g compute networks subnets update "$NETWORK" --region "$REGION" --enable-private-ip-google-access >/dev/null
g compute routers describe factory-router --region "$REGION" >/dev/null 2>&1 \
  || g compute routers create factory-router --region "$REGION" --network "$NETWORK" >/dev/null
g compute routers nats describe factory-nat --router factory-router --region "$REGION" >/dev/null 2>&1 \
  || g compute routers nats create factory-nat --router factory-router --region "$REGION" \
       --auto-allocate-nat-external-ips --nat-all-subnet-ip-ranges >/dev/null

echo "==> node service account (the project has no default compute SA)"
g iam service-accounts describe "$NODE_SA" >/dev/null 2>&1 \
  || g iam service-accounts create "$NODE_SA_NAME" --display-name "GKE nodes for factory-agents" >/dev/null
for role in roles/container.defaultNodeServiceAccount roles/artifactregistry.reader; do
  g projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$NODE_SA" --role "$role" \
    --condition=None >/dev/null
done

echo "==> cluster $CLUSTER"
if ! g container clusters describe "$CLUSTER" --region "$REGION" >/dev/null 2>&1; then
  g container clusters create-auto "$CLUSTER" --region "$REGION" --network "$NETWORK" --subnetwork "$NETWORK" \
    --enable-private-nodes --enable-private-endpoint --no-enable-dns-access \
    --enable-master-authorized-networks --master-authorized-networks "$MAIN_IP/32" \
    --service-account "$NODE_SA"
else
  # keep the one authorized network pointed at the (possibly rebuilt) main node
  g container clusters update "$CLUSTER" --region "$REGION" \
    --enable-master-authorized-networks --master-authorized-networks "$MAIN_IP/32" >/dev/null
fi

echo "==> who may use the cluster: only the main node"
g projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$VM_SA" \
  --role roles/container.developer --condition=None >/dev/null

echo "==> Artifact Registry repo mission-control; factory-1 builds and pushes images"
g artifacts repositories describe mission-control --location "$REGION" >/dev/null 2>&1 \
  || g artifacts repositories create mission-control --location "$REGION" --repository-format docker >/dev/null
g artifacts repositories add-iam-policy-binding mission-control --location "$REGION" \
  --member "serviceAccount:$VM_SA" --role roles/artifactregistry.writer >/dev/null

echo "==> Workload Identity for agents/compliance (no keys)"
KSA="principal://iam.googleapis.com/projects/$PROJECT_NUM/locations/global/workloadIdentityPools/$PROJECT.svc.id.goog/subject/ns/agents/sa/compliance"
for role in roles/aiplatform.user roles/datastore.viewer; do
  g projects add-iam-policy-binding "$PROJECT" --member "$KSA" --role "$role" --condition=None >/dev/null
done
g pubsub topics add-iam-policy-binding "$TOPIC" --member "$KSA" --role roles/pubsub.publisher >/dev/null

echo "==> done. Deploy from the main node: bin/gke/remote-deploy.sh"
