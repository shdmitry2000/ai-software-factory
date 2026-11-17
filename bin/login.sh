#!/usr/bin/env bash
# Sign in once and keep the factory's credentials inside this repo folder only.
#   bin/login.sh            ask for whatever is missing
#   bin/login.sh --redo     ask again for everything
#   bin/login.sh --check    report what is there, change nothing
#
# Everything lands in <repo>/.factory-secrets/ (mode 700, files 600). The tools' own
# config is pointed inside that folder too (GH_CONFIG_DIR, CLAUDE_CONFIG_DIR), so a sign-in
# writes nothing to ~/.config/gh or ~/.claude. The folder is git-ignored and denied to agents
# in .claude/settings.json. Override the location with FACTORY_SECRETS_DIR (must stay inside
# the repo). Tokens are read with `read -s` and never put on a command line.
#
#   gh_token            GitHub token (web sign-in, or a pasted fine-grained token)
#   claude_token        long-lived Claude subscription token (`claude setup-token`)
#   anthropic_api_key   or an Anthropic API key instead of the subscription token
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd -P)
DIR=${FACTORY_SECRETS_DIR:-$ROOT/.factory-secrets}
mkdir -p "$DIR"
DIR=$(cd "$DIR" && pwd -P)
case "$DIR/" in "$ROOT"/*) ;; *) echo "FACTORY_SECRETS_DIR must be inside $ROOT" >&2; exit 1;; esac
chmod 700 "$DIR"
umask 077
MODE=${1:-}

git -C "$ROOT" check-ignore -q "$DIR/x" || { echo "refusing: $DIR is not git-ignored" >&2; exit 1; }

have() { [ -s "$DIR/$1" ]; }
save() {   # save NAME VALUE  (value never echoed)
  printf '%s\n' "$2" > "$DIR/$1.tmp" && chmod 600 "$DIR/$1.tmp" && mv "$DIR/$1.tmp" "$DIR/$1"
}
ask_secret() {  # ask_secret PROMPT -> REPLY (hidden)
  local v; read -r -s -p "$1: " v; echo >&2; REPLY=$v
}
choose() {  # choose "question" opt1 opt2 ... -> REPLY = chosen number
  local q=$1; shift; local i=1
  echo "$q"; for o in "$@"; do echo "  $i) $o"; i=$((i+1)); done
  while :; do read -r -p "choice [1]: " REPLY; REPLY=${REPLY:-1}
    [[ $REPLY =~ ^[0-9]+$ ]] && [ "$REPLY" -ge 1 ] && [ "$REPLY" -le $# ] && return; done
}

github_status() {
  have gh_token || { echo "github: missing"; return 1; }
  local who; who=$(GH_CONFIG_DIR="$DIR/gh" GH_TOKEN=$(cat "$DIR/gh_token") gh api user -q .login 2>/dev/null) \
    && echo "github: ok ($who)" || { echo "github: token rejected"; return 1; }
}
claude_status() {
  if have claude_token; then
    [[ $(cat "$DIR/claude_token") == sk-ant-oat* ]] && echo "claude: subscription token" || { echo "claude: claude_token has an unexpected format"; return 1; }
  elif have anthropic_api_key; then
    [[ $(cat "$DIR/anthropic_api_key") == sk-ant-* ]] && echo "claude: API key" || { echo "claude: anthropic_api_key has an unexpected format"; return 1; }
  else echo "claude: missing"; return 1; fi
}

if [ "$MODE" = --check ]; then
  echo "secrets: $DIR"; rc=0
  github_status || rc=1; claude_status || rc=1; exit $rc
fi
[ "$MODE" = --redo ] && rm -f "$DIR/gh_token" "$DIR/claude_token" "$DIR/anthropic_api_key"

# ---- GitHub ---------------------------------------------------------------
if ! github_status >/dev/null 2>&1; then
  choose "GitHub sign-in for the factory:" \
    "Sign in with the browser (gh web login; token scoped repo + workflow)" \
    "Paste a fine-grained token (only the app repo: Contents, Pull requests, Workflows read/write)"
  if [ "$REPLY" = 1 ]; then
    rm -rf "$DIR/gh"
    GH_CONFIG_DIR="$DIR/gh" gh auth login --hostname github.com --git-protocol https --web -s workflow
    save gh_token "$(GH_CONFIG_DIR="$DIR/gh" gh auth token --hostname github.com)"
  else
    echo "Create it at https://github.com/settings/personal-access-tokens/new"
    ask_secret "GitHub token (hidden)"; save gh_token "$REPLY"
  fi
  github_status || { rm -f "$DIR/gh_token"; echo "GitHub token did not work; run again." >&2; exit 1; }
fi

# ---- Claude / coding agent -----------------------------------------------
if ! claude_status >/dev/null 2>&1; then
  choose "Coding agent sign-in:" \
    "Sign in with your Claude subscription (claude setup-token, opens the browser)" \
    "Paste a long-lived token you already made with claude setup-token" \
    "Paste an Anthropic API key (Console, pay per use)"
  case $REPLY in
    1) echo "A browser opens. When the token is printed, copy it; you will paste it next."
       CLAUDE_CONFIG_DIR="$DIR/claude" claude setup-token || true
       ask_secret "Paste the token (hidden)"; save claude_token "$REPLY"; rm -f "$DIR/anthropic_api_key";;
    2) ask_secret "Claude token (hidden)"; save claude_token "$REPLY"; rm -f "$DIR/anthropic_api_key";;
    3) ask_secret "Anthropic API key (hidden)"; save anthropic_api_key "$REPLY"; rm -f "$DIR/claude_token";;
  esac
  claude_status || { rm -f "$DIR/claude_token" "$DIR/anthropic_api_key"; echo "That did not look like a Claude token or API key; run again." >&2; exit 1; }
fi

echo "secrets: $DIR"; github_status; claude_status
echo "Next: bin/gcp/push-logins.sh   (or bin/gcp/rebuild.sh after a sweep)"
