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
  artifactregistry.googleapis.com cloudscheduler.googleapis.com $G

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
for sa in factory-vm factory-writer; do
  gcloud iam service-accounts describe $sa@$PROJECT.iam.gserviceaccount.com $G >/dev/null 2>&1 ||
    gcloud iam service-accounts create $sa --display-name "mission control: $sa" $G
done
VM_SA=factory-vm@$PROJECT.iam.gserviceaccount.com
WRITER_SA=factory-writer@$PROJECT.iam.gserviceaccount.com

say "IAM"
# A new service account takes a while to become visible to IAM; retry instead of failing.
retry() { for i in 1 2 3 4 5 6; do "$@" >/dev/null 2>&1 && return 0; sleep 10; done; "$@" >/dev/null; }
retry gcloud pubsub topics add-iam-policy-binding factory-events $G \
  --member serviceAccount:$VM_SA --role roles/pubsub.publisher
retry gcloud pubsub topics add-iam-policy-binding factory-commands $G \
  --member serviceAccount:$WRITER_SA --role roles/pubsub.publisher
for sa in $VM_SA $WRITER_SA; do   # the bridge keeps its cursor in engines/{engine}
  retry gcloud projects add-iam-policy-binding $PROJECT --member serviceAccount:$sa \
    --role roles/datastore.user --condition=None
done

say "Artifact Registry"
gcloud artifacts repositories describe mission-control --location $REGION $G >/dev/null 2>&1 ||
  gcloud artifacts repositories create mission-control --repository-format docker --location $REGION $G

say "done"
