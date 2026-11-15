#!/usr/bin/env bash
# Rebuild the whole factory server after the weekend sandbox cleaner deleted it.
#   bin/gcp/rebuild.sh            (from the factory repo root, on the Mac)
# Everything the factory needs between rebuilds lives in GitHub (app repo with its
# committed factory files) and in ~/.factory-secrets on the Mac.
set -euo pipefail
cd "$(dirname "$0")/../.."
export PROJECT=${PROJECT:-me-sb-dgcp-dpoc-pocyosh-pr} ZONE=${ZONE:-me-west1-a} VM=${VM:-factory-1}
F="--project $PROJECT --zone $ZONE --tunnel-through-iap --quiet"

bin/gcp/create-factory-vm.sh bin/bootstrap-ubuntu.sh
gcloud compute scp bin/gcp/setup-app-on-vm.sh "$VM":/tmp/ $F
gcloud compute ssh "$VM" $F --command 'sudo -i bash /tmp/setup-app-on-vm.sh'
bin/gcp/push-logins.sh
gcloud compute scp bin/gcp/archon-console.sh "$VM":/tmp/ $F
gcloud compute ssh "$VM" $F --command 'sudo -i bash /tmp/archon-console.sh' < /dev/null
gcloud compute ssh "$VM" $F --command 'sudo -i bash -lc "cd ~/file-to-markdown-convertor && python3 factory/consumer.py doctor"'
