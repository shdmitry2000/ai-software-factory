#!/usr/bin/env bash
# Install the GitHub and Claude Code logins on the VM from the credentials bin/login.sh
# keeps in <repo>/.factory-secrets, so a rebuilt VM needs no interactive login:
#   gh_token                         GitHub token
#   claude_token | anthropic_api_key  Claude subscription token, or an Anthropic API key
# Run bin/login.sh first. Tokens go over stdin, never on a command line.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd -P)
PROJECT=${PROJECT:-me-sb-dgcp-dpoc-pocyosh-pr}
ZONE=${ZONE:-me-west1-a}
VM=${VM:-factory-1}
DIR=${FACTORY_SECRETS_DIR:-$ROOT/.factory-secrets}
"$ROOT/bin/login.sh" --check >/dev/null || {
  echo "credentials missing or invalid in $DIR; run bin/login.sh" >&2; exit 1; }
if [ -s "$DIR/claude_token" ]; then VAR=CLAUDE_CODE_OAUTH_TOKEN; FILE=claude_token
else VAR=ANTHROPIC_API_KEY; FILE=anthropic_api_key; fi
SSH="gcloud compute ssh $VM --project $PROJECT --zone $ZONE --tunnel-through-iap --quiet"

cat "$DIR/gh_token" | $SSH --command 'sudo -i bash -lc "gh auth login --hostname github.com --with-token && gh auth setup-git && gh auth status 2>&1 | head -3"'
printf 'export %s=%s\n' "$VAR" "$(cat "$DIR/$FILE")" |
  $SSH --command 'sudo -i bash -c "umask 077; cat > ~/.factory-env; grep -q factory-env ~/.profile || echo \"source ~/.factory-env; export IS_SANDBOX=1\" >> ~/.profile; echo claude credential installed"'
