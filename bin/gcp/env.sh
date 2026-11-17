# Sourced by every Mac-side script that talks to GCP. Keeps gcloud's login and config,
# the SSH key and known-hosts file it makes for `gcloud compute ssh`, and docker's registry
# login inside <repo>/.factory-secrets, so nothing is written to ~/.config/gcloud, ~/.ssh
# or ~/.docker. Sign in with bin/login.sh.
FACTORY_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)
FACTORY_SECRETS_DIR=${FACTORY_SECRETS_DIR:-$FACTORY_ROOT/.factory-secrets}
export FACTORY_ROOT FACTORY_SECRETS_DIR
export CLOUDSDK_CONFIG=$FACTORY_SECRETS_DIR/gcloud
export DOCKER_CONFIG=$FACTORY_SECRETS_DIR/docker
# gcloud derives ~/.ssh/google_compute_* from HOME; give it a home inside the folder.
gcloud() { HOME="$FACTORY_SECRETS_DIR/home" command gcloud "$@"; }
export -f gcloud
