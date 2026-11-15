#!/usr/bin/env bash
# Install the GitHub and Claude Code logins on the VM from tokens kept on this Mac, so a
# rebuilt VM needs no interactive login. Tokens live in ~/.factory-secrets (mode 700/600):
#   gh_token      a GitHub token with repo + workflow scope (fine-grained: the app repo only)
#   claude_token  output of `claude setup-token`
set -euo pipefail
PROJECT=${PROJECT:-me-sb-dgcp-dpoc-pocyosh-pr}
ZONE=${ZONE:-me-west1-a}
VM=${VM:-factory-1}
DIR=${FACTORY_SECRETS:-$HOME/.factory-secrets}
for f in gh_token claude_token; do
  [ -s "$DIR/$f" ] || { echo "missing $DIR/$f" >&2; exit 1; }
done
SSH="gcloud compute ssh $VM --project $PROJECT --zone $ZONE --tunnel-through-iap --quiet"

# Tokens go over stdin, never on a command line or into the shell history.
{ cat "$DIR/gh_token"; } | $SSH --command 'sudo -i bash -lc "gh auth login --hostname github.com --with-token && gh auth setup-git && gh auth status 2>&1 | head -3"'
{ printf 'export CLAUDE_CODE_OAUTH_TOKEN=%s\n' "$(cat "$DIR/claude_token")"; } |
  $SSH --command 'sudo -i bash -c "umask 077; cat > ~/.factory-env; grep -q factory-env ~/.profile || echo \"source ~/.factory-env; export IS_SANDBOX=1\" >> ~/.profile; echo claude token installed"'
