#!/usr/bin/env bash
# Create the factory VM on GCE and install the toolchain. Idempotent.
#   PROJECT=... ZONE=... VM=... ./create-factory-vm.sh /path/to/bootstrap-ubuntu.sh
set -euo pipefail
source "$(dirname "$0")/env.sh"   # gcloud/docker config inside .factory-secrets
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

# No rule for Archon's console (tcp:3090) or the factory console: both are reached only
# through the SSH connection that bin/gcp/console.sh opens (console UI-0.5). Remove the
# old rule if an earlier version of this script made it.
gcloud compute firewall-rules describe allow-iap-archon-factory $G >/dev/null 2>&1 &&
  gcloud compute firewall-rules delete allow-iap-archon-factory $G --quiet

# Service account factory-vm (made by mission-control/infra/setup.sh): may publish to
# factory-events and keep its bridge cursor in Firestore, nothing else. The project has
# no default compute SA. Scope cloud-platform: IAM roles, not scopes, limit it.
VM_SA=factory-vm@$PROJECT.iam.gserviceaccount.com
gcloud compute instances describe $VM $G --zone $ZONE >/dev/null 2>&1 ||
  gcloud compute instances create $VM $G --zone $ZONE --machine-type $TYPE \
    --image-family ubuntu-2404-lts-amd64 --image-project ubuntu-os-cloud \
    --boot-disk-size 100GB --boot-disk-type pd-balanced \
    --tags factory --labels purpose=ai-software-factory \
    --service-account "$VM_SA" --scopes cloud-platform

# An existing VM created before mission control: attach the account (needs a stop).
if [ "$(gcloud compute instances describe $VM $G --zone $ZONE --format='value(serviceAccounts[0].email)')" != "$VM_SA" ]; then
  gcloud compute instances stop $VM $G --zone $ZONE --quiet
  gcloud compute instances set-service-account $VM $G --zone $ZONE \
    --service-account "$VM_SA" --scopes cloud-platform
  gcloud compute instances start $VM $G --zone $ZONE --quiet
fi

# Wait for sshd.
for i in $(seq 1 30); do $SSH --command true >/dev/null 2>&1 && break; sleep 10; done

gcloud compute scp "$BOOTSTRAP" $VM:/tmp/bootstrap-ubuntu.sh $G --zone $ZONE --tunnel-through-iap --quiet
# Claude Code's installer refuses sudo without CLAUDE_INSTALL_ALLOW_SUDO=1.
$SSH --command 'sudo -i CLAUDE_INSTALL_ALLOW_SUDO=1 bash /tmp/bootstrap-ubuntu.sh' | tail -12
