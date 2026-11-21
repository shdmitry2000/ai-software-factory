#!/usr/bin/env bash
# From the Mac: ship this repo's committed HEAD to the main node and run
# bin/agents/deploy.sh there. Only the main node builds and runs containers.
#   bin/agents/remote-deploy.sh            # env: LLM=project|api_key|none
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd -P)
source "$ROOT/bin/gcp/env.sh"
PROJECT=${PROJECT:-me-sb-dgcp-dpoc-pocyosh-pr}
ZONE=${ZONE:-me-west1-a}
VM=${MAIN_VM:-factory-1}
BUNDLE=$(mktemp -d "$ROOT/.factory-secrets/tmp.XXXX")/factory.bundle   # stays in the repo folder
trap 'rm -rf "$(dirname "$BUNDLE")"' EXIT

git -C "$ROOT" bundle create "$BUNDLE" HEAD >/dev/null 2>&1
gcloud compute scp "$BUNDLE" "$VM:/tmp/factory.bundle" --zone "$ZONE" --project "$PROJECT" --tunnel-through-iap
# The script goes over stdin: no nested quoting for sudo or a login shell to re-parse.
gcloud compute ssh "$VM" --zone "$ZONE" --project "$PROJECT" --tunnel-through-iap --command "sudo bash -s" <<EOF
set -e
D=/root/factory-deploy
if [ -d \$D/.git ]; then git -C \$D fetch -q /tmp/factory.bundle HEAD && git -C \$D checkout -q --detach FETCH_HEAD
else git clone -q /tmp/factory.bundle \$D; fi
rm -f /tmp/factory.bundle
LLM=${LLM:-project} bash \$D/bin/agents/deploy.sh
EOF
