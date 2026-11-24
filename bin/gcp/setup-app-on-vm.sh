#!/usr/bin/env bash
# On the VM, as root: clone the factory and the app, install the app's deps, install the factory.
#   sudo -i bash setup-app-on-vm.sh [bundle-file branch]
set -euo pipefail
export PATH="$HOME/.bun/bin:$HOME/.local/bin:$PATH"
FACTORY_REPO=${FACTORY_REPO:-https://github.com/shdmitry2000/ai-software-factory}
APP_REPO=${APP_REPO:-https://github.com/shdmitry2000/file-to-markdown-convertor}
APP_DIR=${APP_DIR:-$HOME/file-to-markdown-convertor}

[ -d ~/ai-software-factory ] || git clone -q "$FACTORY_REPO" ~/ai-software-factory
git -C ~/ai-software-factory pull -q --ff-only
[ -d "$APP_DIR" ] || git clone -q "$APP_REPO" "$APP_DIR"
cd "$APP_DIR"
if [ $# -eq 2 ]; then               # an unpushed branch, carried over as a git bundle
  git fetch -q "$1" "$2:$2" && git switch -q "$2"
fi
git log --oneline -1
uv sync -q --extra test
uv run python -m app.shared_check && echo "shared_check ok"
python3 ~/ai-software-factory/bin/factory.py init | tail -2
