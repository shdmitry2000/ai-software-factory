#!/usr/bin/env bash
# Build, push and deploy the writer, then wire factory-events → writer with a dead-letter
# topic, and the scheduled problem rules (Cloud Scheduler → POST /rules, plan F7). Idempotent. Run from the factory repo root after infra/setup.sh.
#   PROJECT=... REGION=... mission-control/infra/deploy-writer.sh
set -euo pipefail
source "$(dirname "$0")/../../bin/gcp/env.sh"   # gcloud/docker config inside .factory-secrets
export CLOUDSDK_CORE_DISABLE_PROMPTS=1
PROJECT=${PROJECT:-me-sb-dgcp-dpoc-pocyosh-pr}
REGION=${REGION:-me-west1}
G="--project $PROJECT"
REPO=$REGION-docker.pkg.dev/$PROJECT/mission-control
TAG=${TAG:-$(git rev-parse --short HEAD)}
WRITER_SA=factory-writer@$PROJECT.iam.gserviceaccount.com
NUM=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
PUBSUB_AGENT=service-$NUM@gcp-sa-pubsub.iam.gserviceaccount.com
say() { printf '\n==> %s\n' "$*"; }

say "image $REPO/writer:$TAG"
gcloud auth configure-docker "$REGION-docker.pkg.dev" --quiet >/dev/null 2>&1
docker build -q --platform linux/amd64 -t "$REPO/writer:$TAG" "$(dirname "$0")/.." >/dev/null
docker push -q "$REPO/writer:$TAG" >/dev/null

say "Cloud Run service (private)"
gcloud run deploy mission-control-writer $G --region "$REGION" --image "$REPO/writer:$TAG" \
  --service-account "$WRITER_SA" --no-allow-unauthenticated --set-env-vars "GCP_PROJECT=$PROJECT" \
  --cpu 1 --memory 512Mi --min-instances 0 --max-instances 3 --quiet >/dev/null
URL=$(gcloud run services describe mission-control-writer $G --region "$REGION" --format='value(status.url)')
echo "$URL"

say "push identity and dead-letter permissions"
# The writer's own service account signs the push requests; only it may invoke the service.
gcloud run services add-iam-policy-binding mission-control-writer $G --region "$REGION" \
  --member "serviceAccount:$WRITER_SA" --role roles/run.invoker >/dev/null
gcloud iam service-accounts add-iam-policy-binding "$WRITER_SA" $G \
  --member "serviceAccount:$PUBSUB_AGENT" --role roles/iam.serviceAccountTokenCreator >/dev/null
gcloud pubsub topics add-iam-policy-binding factory-events-dlq $G \
  --member "serviceAccount:$PUBSUB_AGENT" --role roles/pubsub.publisher >/dev/null

say "subscriptions"
# Dead-lettered messages are kept here (a topic alone would drop them) for the Problems view.
gcloud pubsub subscriptions describe factory-events-dlq-hold $G >/dev/null 2>&1 ||
  gcloud pubsub subscriptions create factory-events-dlq-hold $G --topic factory-events-dlq \
    --message-retention-duration 7d --expiration-period never >/dev/null
SUB_ARGS=(--push-endpoint "$URL/" --push-auth-service-account "$WRITER_SA"
  --dead-letter-topic factory-events-dlq --max-delivery-attempts 10
  --min-retry-delay 10s --max-retry-delay 60s --ack-deadline 30)
if gcloud pubsub subscriptions describe factory-events-writer $G >/dev/null 2>&1; then
  gcloud pubsub subscriptions update factory-events-writer $G "${SUB_ARGS[@]}" >/dev/null
else
  gcloud pubsub subscriptions create factory-events-writer $G --topic factory-events \
    --enable-message-ordering --expiration-period never "${SUB_ARGS[@]}" >/dev/null
fi
gcloud pubsub subscriptions add-iam-policy-binding factory-events-writer $G \
  --member "serviceAccount:$PUBSUB_AGENT" --role roles/pubsub.subscriber >/dev/null

say "scheduled problem rules (every 2 min)"
# The rules publish problem.opened/resolved on factory-events and alert through the
# notifier (Cloud Logging lines labelled factory_alert=1). The scheduler signs its
# calls as the writer's service account, the only invoker of the service.
gcloud services enable cloudscheduler.googleapis.com $G >/dev/null
gcloud pubsub topics add-iam-policy-binding factory-events $G \
  --member "serviceAccount:$WRITER_SA" --role roles/pubsub.publisher >/dev/null
SCHED_REGION=${SCHED_REGION:-$REGION}
JOB_ARGS=(--location "$SCHED_REGION" --schedule "*/2 * * * *" --time-zone Etc/UTC
  --uri "$URL/rules" --http-method POST --oidc-service-account-email "$WRITER_SA" --oidc-token-audience "$URL"
  --attempt-deadline 60s --max-retry-attempts 0)
if gcloud scheduler jobs describe factory-problem-rules $G --location "$SCHED_REGION" >/dev/null 2>&1; then
  gcloud scheduler jobs update http factory-problem-rules $G "${JOB_ARGS[@]}" >/dev/null
else
  gcloud scheduler jobs create http factory-problem-rules $G "${JOB_ARGS[@]}" >/dev/null
fi
echo "factory-problem-rules in $SCHED_REGION → $URL/rules"

say "done"
