#!/usr/bin/env bash
# Rebuild the whole factory server after the weekend sandbox cleaner deleted it.
#   bin/gcp/rebuild.sh            (from the factory repo root, on the Mac)
# Everything the factory needs between rebuilds lives in GitHub (app repo with its
# committed factory files) and in .factory-secrets/ in this folder (bin/login.sh).
set -euo pipefail
source "$(dirname "$0")/env.sh"   # gcloud/docker config inside .factory-secrets
cd "$(dirname "$0")/../.."
export PROJECT=${PROJECT:-me-sb-dgcp-dpoc-pocyosh-pr} ZONE=${ZONE:-me-west1-a} VM=${VM:-factory-1}
F="--project $PROJECT --zone $ZONE --tunnel-through-iap --quiet"

# The weekend sweep disables APIs (Pub/Sub, Firestore, Cloud Run, IAM, ...) and deletes VMs,
# disks and firewall rules; topics, Firestore data, the Cloud Run service and images survive.
# setup.sh re-enables the APIs; deploy-writer.sh re-applies the push wiring (idempotent).
mission-control/infra/setup.sh < /dev/null
mission-control/infra/deploy-writer.sh < /dev/null
bin/gcp/create-factory-vm.sh bin/bootstrap-ubuntu.sh
gcloud compute scp bin/gcp/setup-app-on-vm.sh "$VM":/tmp/ $F
gcloud compute ssh "$VM" $F --command 'sudo -i bash /tmp/setup-app-on-vm.sh'
gcloud compute scp bin/gcp/archon-console.sh "$VM":/tmp/ $F
gcloud compute ssh "$VM" $F --command 'sudo -i bash /tmp/archon-console.sh' < /dev/null
# Mission control bridge (resumes from the cursor it saved in Firestore before the sweep).
gcloud compute scp mission-control/bridge/install-on-vm.sh "$VM":/tmp/ $F
gcloud compute ssh "$VM" $F --command "sudo -i MC_BRANCH=${MC_BRANCH:-mission-control} bash /tmp/install-on-vm.sh --service" < /dev/null
bin/login.sh             # asks to sign in only if a credential is missing
bin/gcp/push-logins.sh
gcloud compute ssh "$VM" $F --command 'sudo -i bash -lc "cd ~/file-to-markdown-convertor && python3 factory/consumer.py doctor"'
