#!/usr/bin/env bash
# Create the factory VM on GCE and install the toolchain. Idempotent.
#   PROJECT=... ZONE=... VM=... ./create-factory-vm.sh /path/to/bootstrap-ubuntu.sh
set -euo pipefail
PROJECT=${PROJECT:-me-sb-dgcp-dpoc-pocyosh-pr}
ZONE=${ZONE:-me-west1-a}
VM=${VM:-factory-1}
TYPE=${TYPE:-e2-standard-2}
BOOTSTRAP=${1:?path to bin/bootstrap-ubuntu.sh}
G="--project $PROJECT"
SSH="gcloud compute ssh $VM $G --zone $ZONE --tunnel-through-iap --quiet"

# SSH only from Google's IAP range; no port 22 open to the internet.
gcloud compute firewall-rules describe allow-iap-ssh-factory $G >/dev/null 2>&1 ||
  gcloud compute firewall-rules create allow-iap-ssh-factory $G --network default \
    --direction INGRESS --allow tcp:22 --source-ranges 35.235.240.0/20 --target-tags factory

# No service account: the project has no default compute SA and the VM needs no GCP APIs.
gcloud compute instances describe $VM $G --zone $ZONE >/dev/null 2>&1 ||
  gcloud compute instances create $VM $G --zone $ZONE --machine-type $TYPE \
    --image-family ubuntu-2404-lts-amd64 --image-project ubuntu-os-cloud \
    --boot-disk-size 100GB --boot-disk-type pd-balanced \
    --tags factory --labels purpose=ai-software-factory \
    --no-service-account --no-scopes

# Wait for sshd.
for i in $(seq 1 30); do $SSH --command true >/dev/null 2>&1 && break; sleep 10; done

gcloud compute scp "$BOOTSTRAP" $VM:/tmp/bootstrap-ubuntu.sh $G --zone $ZONE --tunnel-through-iap --quiet
# Claude Code's installer refuses sudo without CLAUDE_INSTALL_ALLOW_SUDO=1.
$SSH --command 'sudo -i CLAUDE_INSTALL_ALLOW_SUDO=1 bash /tmp/bootstrap-ubuntu.sh' | tail -12
