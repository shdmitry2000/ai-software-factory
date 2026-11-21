#!/usr/bin/env bash
# Mission control GCP resources. Idempotent: rerun after any sweep.
#   PROJECT=... REGION=... mission-control/infra/setup.sh
set -euo pipefail
source "$(dirname "$0")/../../bin/gcp/env.sh"   # gcloud/docker config inside .factory-secrets
export CLOUDSDK_CORE_DISABLE_PROMPTS=1   # a prompt would hang an unattended run
PROJECT=${PROJECT:-me-sb-dgcp-dpoc-pocyosh-pr}
REGION=${REGION:-me-west1}
FS_LOCATION=${FS_LOCATION:-$REGION}
G="--project $PROJECT"
say() { printf '\n==> %s\n' "$*"; }

say "APIs"
gcloud services enable firestore.googleapis.com pubsub.googleapis.com run.googleapis.com \
  artifactregistry.googleapis.com cloudscheduler.googleapis.com compute.googleapis.com oslogin.googleapis.com $G

say "Firestore (native mode, $FS_LOCATION)"
# The database survives the weekend sweep, but right after the API is re-enabled
# `describe` can still say SERVICE_DISABLED; "already exists" from create is success.
if ! gcloud firestore databases describe --database='(default)' $G >/dev/null 2>&1; then
  out=$(gcloud firestore databases create --database='(default)' --location="$FS_LOCATION" \
        --type=firestore-native $G 2>&1) || {
    grep -q "already exists" <<<"$out" || { echo "$out" >&2; exit 1; }
    echo "    (default) already exists"; }
fi

say "Pub/Sub topics"
for t in factory-events factory-events-dlq factory-commands; do
  gcloud pubsub topics describe $t $G >/dev/null 2>&1 || gcloud pubsub topics create $t $G
done

say "Service accounts"
for sa in factory-vm factory-writer factory-agent; do
  gcloud iam service-accounts describe $sa@$PROJECT.iam.gserviceaccount.com $G >/dev/null 2>&1 ||
    gcloud iam service-accounts create $sa --display-name "mission control: $sa" $G
done
VM_SA=factory-vm@$PROJECT.iam.gserviceaccount.com
WRITER_SA=factory-writer@$PROJECT.iam.gserviceaccount.com
AGENT_SA=factory-agent@$PROJECT.iam.gserviceaccount.com

say "IAM"
# A new service account takes a while to become visible to IAM; retry instead of failing.
retry() { for i in 1 2 3 4 5 6; do "$@" >/dev/null 2>&1 && return 0; sleep 10; done; "$@" >/dev/null; }
retry gcloud pubsub topics add-iam-policy-binding factory-events $G \
  --member serviceAccount:$VM_SA --role roles/pubsub.publisher
retry gcloud pubsub topics add-iam-policy-binding factory-commands $G \
  --member serviceAccount:$WRITER_SA --role roles/pubsub.publisher
# Commands: the console and CLI on factory-1 send on factory-commands; the handler on
# factory-1 pulls them from its own subscription.
gcloud pubsub subscriptions describe factory-commands-handler $G >/dev/null 2>&1 ||
  gcloud pubsub subscriptions create factory-commands-handler --topic factory-commands \
    --ack-deadline 60 --message-retention-duration 1d $G >/dev/null
retry gcloud pubsub topics add-iam-policy-binding factory-commands $G \
  --member serviceAccount:$VM_SA --role roles/pubsub.publisher
retry gcloud pubsub subscriptions add-iam-policy-binding factory-commands-handler $G \
  --member serviceAccount:$VM_SA --role roles/pubsub.subscriber
for sa in $VM_SA $WRITER_SA; do   # the bridge keeps its cursor in engines/{engine}
  retry gcloud projects add-iam-policy-binding $PROJECT --member serviceAccount:$sa \
    --role roles/datastore.user --condition=None
done

say "Agent hosts (VMs agent-* that the console creates; docs/deploy-and-hosts-plan.md §3)"
# The agents on those VMs run as factory-agent: publish events, read and write the store.
retry gcloud pubsub topics add-iam-policy-binding factory-events $G \
  --member serviceAccount:$AGENT_SA --role roles/pubsub.publisher
retry gcloud projects add-iam-policy-binding $PROJECT --member serviceAccount:$AGENT_SA \
  --role roles/datastore.user --condition=None
# The command handler on factory-1 (factory-vm) creates, logs in to and deletes those VMs,
# and only those: both grants are limited to instances and disks named agent-*.
retry gcloud iam service-accounts add-iam-policy-binding $AGENT_SA $G \
  --member serviceAccount:$VM_SA --role roles/iam.serviceAccountUser
AGENT_ONLY='(resource.type != "compute.googleapis.com/Instance" && resource.type != "compute.googleapis.com/Disk") || resource.name.extract("/instances/{name}").startsWith("agent-") || resource.name.extract("/disks/{name}").startsWith("agent-")'
retry gcloud projects add-iam-policy-binding $PROJECT --member serviceAccount:$VM_SA \
  --role roles/compute.instanceAdmin.v1 \
  --condition="expression=$AGENT_ONLY,title=factory-agent-hosts-only,description=VMs and disks named agent-*"
retry gcloud projects add-iam-policy-binding $PROJECT --member serviceAccount:$VM_SA \
  --role roles/compute.osAdminLogin \
  --condition="expression=resource.name.extract(\"/instances/{name}\").startsWith(\"agent-\"),title=factory-agent-hosts-login,description=OS Login as admin on agent-* VMs only"

say "Artifact Registry"
gcloud artifacts repositories describe mission-control --location $REGION $G >/dev/null 2>&1 ||
  gcloud artifacts repositories create mission-control --repository-format docker --location $REGION $G

say "Firestore indexes (console counts: Chains edges per day)"
# Each create returns at once (--async); "already exists" is success.
index() {
  out=$(gcloud firestore indexes composite create --collection-group=events --async "$@" $G 2>&1) || {
    grep -qi "already exists" <<<"$out" || { echo "$out" >&2; exit 1; }; }
}
index --field-config field-path=type,order=ascending --field-config field-path=received_at,order=ascending
index --field-config field-path=type,order=ascending --field-config field-path=factoryengine,order=ascending \
      --field-config field-path=received_at,order=ascending

say "done"
