# Sourced by every Mac-side script that talks to GCP, and by hand (bash or zsh) before
# typing gcloud commands. Keeps gcloud's login and config, the SSH key and known-hosts file
# it makes for `gcloud compute ssh`, and docker's registry login inside
# <repo>/.factory-secrets, so nothing is written to ~/.config/gcloud, ~/.ssh or ~/.docker.
# Sign in with bin/login.sh.
if [ -n "${ZSH_VERSION:-}" ]; then eval '_factory_env=${(%):-%x}'; else _factory_env=${BASH_SOURCE[0]}; fi
FACTORY_ROOT=$(cd "$(dirname "$_factory_env")/../.." && pwd -P)
unset _factory_env
FACTORY_SECRETS_DIR=${FACTORY_SECRETS_DIR:-$FACTORY_ROOT/.factory-secrets}
export FACTORY_ROOT FACTORY_SECRETS_DIR
export CLOUDSDK_CONFIG=$FACTORY_SECRETS_DIR/gcloud
export DOCKER_CONFIG=$FACTORY_SECRETS_DIR/docker
export KUBECONFIG=$FACTORY_SECRETS_DIR/home/.kube/config
# gcloud derives ~/.ssh/google_compute_* from HOME; give it a home inside the folder.
gcloud() { HOME="$FACTORY_SECRETS_DIR/home" command gcloud "$@"; }
[ -n "${BASH_VERSION:-}" ] && export -f gcloud
true
