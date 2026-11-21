#!/usr/bin/env bash
# On the factory VM, as root: run a bash-only Archon workflow (fan-out + join, no AI login
# needed) so the bridge has real runs and events to read. sudo -i bash probe.sh

set -e
export PATH="$HOME/.bun/bin:$HOME/.local/bin:$PATH" ARCHON_HOME=$HOME/.archon
P=$HOME/mc-probe
if [ ! -d $P/.git ]; then
  mkdir -p $P/.archon/workflows && cd $P && git init -q && git config user.email probe@factory.local && git config user.name probe
  cat > .archon/workflows/mc-probe.yaml <<'WF'
name: mc-probe
description: Bash-only probe with a fan-out and join, for mission control's bridge tests.
nodes:
  - id: pick
    bash: echo picked
  - id: build
    depends_on: [pick]
    bash: sleep 2 && echo built
  - id: validate
    depends_on: [build]
    bash: sleep 3 && echo tests-ok
  - id: app-check
    depends_on: [build]
    bash: sleep 4 && echo app-ok
  - id: holdout-verify
    depends_on: [build]
    bash: sleep 2 && echo holdouts-ok
  - id: review
    depends_on: [validate, app-check, holdout-verify]
    bash: echo ready
  - id: merge-gate
    depends_on: [review]
    bash: echo merged
WF
  echo "# probe" > README.md && git add -A && git commit -qm probe
fi
cd $P && timeout 300 bun /root/archon-console/packages/cli/src/cli.ts workflow run mc-probe --cwd $P --no-worktree 2>&1 | grep -v "\"level\":40" | tail -15
