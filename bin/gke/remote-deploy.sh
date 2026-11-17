#!/usr/bin/env bash
# From the Mac: ship this repo's committed HEAD to the main node and run deploy.sh there.
# The Mac never talks to the cluster; only factory-1 does.
#   bin/gke/remote-deploy.sh            # env: VERTEX_REGION=global|eu|me-west1
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd -P)
source "$ROOT/bin/gcp/env.sh"
PROJECT=${PROJECT:-me-sb-dgcp-dpoc-pocyosh-pr}
ZONE=${ZONE:-me-west1-a}
VM=${MAIN_VM:-factory-1}
BUNDLE=$(mktemp -d "$ROOT/.factory-secrets/tmp.XXXX")/factory.bundle   # stays in the repo folder
trap 'rm -rf "$(dirname "$BUNDLE")"' EXIT

git -C "$ROOT" bundle create "$BUNDLE" HEAD >/dev/null 2>&1
ssh_() { gcloud compute ssh "$VM" --zone "$ZONE" --project "$PROJECT" --tunnel-through-iap "$@"; }
gcloud compute scp "$BUNDLE" "$VM:/tmp/factory.bundle" --zone "$ZONE" --project "$PROJECT" --tunnel-through-iap
ssh_ --command "sudo bash -c 'set -e
  D=/root/factory-deploy
  if [ -d \$D/.git ]; then git -C \$D fetch -q /tmp/factory.bundle HEAD && git -C \$D checkout -q --detach FETCH_HEAD
  else git clone -q /tmp/factory.bundle \$D; fi
  rm -f /tmp/factory.bundle
  VERTEX_REGION=${VERTEX_REGION:-global} bash \$D/bin/gke/deploy.sh'"
