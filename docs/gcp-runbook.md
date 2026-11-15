# Factory server on GCP: runbook

How to bring up a factory server on Google Compute Engine and connect an app repo to it,
from a Mac with `gcloud` signed in. First done 2026-11-14/15 for
`shdmitry2000/file-to-markdown-convertor`; every gotcha we hit is already handled by the
scripts below. About 25 minutes, most of it package downloads.

| Setting | Value we used |
|---|---|
| Project | `me-sb-dgcp-dpoc-pocyosh-pr` (sandbox, see [the warning](#warning-sandbox-cleaner)) |
| Zone | `me-west1-a` |
| VM | `factory-1`, `e2-standard-2` (2 vCPU, 8 GB), Ubuntu 24.04, 100 GB pd-balanced |
| Access | SSH through IAP only; no port 22 open to the internet |
| Runs as | `root` (matches the factory's systemd timer and `IS_SANDBOX=1` path) |

## 1. Create the VM and install the toolchain (Mac, ~8 min)

```bash
cd ~/code/software_factory/ai-software-factory
PROJECT=me-sb-dgcp-dpoc-pocyosh-pr ZONE=me-west1-a VM=factory-1 TYPE=e2-standard-2 \
  bin/gcp/create-factory-vm.sh bin/bootstrap-ubuntu.sh
```

[`bin/gcp/create-factory-vm.sh`](../bin/gcp/create-factory-vm.sh) is idempotent. It:

- creates firewall rule `allow-iap-ssh-factory` (tcp:22 from `35.235.240.0/20`, tag `factory`);
- creates firewall rule `allow-iap-archon-factory` (tcp:3090, IAP only) for the Archon console;
- creates the VM with service account `factory-vm` (made by `mission-control/infra/setup.sh`), or attaches it to an existing VM;
- copies [`bin/bootstrap-ubuntu.sh`](../bin/bootstrap-ubuntu.sh) over and runs it as root:
  git, python3, gh, caddy, bun, uv, Claude Code, Codex.

Every later command reaches the VM with:

```bash
gcloud compute ssh factory-1 --project me-sb-dgcp-dpoc-pocyosh-pr --zone me-west1-a --tunnel-through-iap
```

## 2. Clone the app and install the factory (Mac, ~10 min)

```bash
F="--project me-sb-dgcp-dpoc-pocyosh-pr --zone me-west1-a --tunnel-through-iap --quiet"
gcloud compute scp bin/gcp/setup-app-on-vm.sh factory-1:/tmp/ $F
gcloud compute ssh factory-1 $F --command 'sudo -i bash /tmp/setup-app-on-vm.sh'
```

[`bin/gcp/setup-app-on-vm.sh`](../bin/gcp/setup-app-on-vm.sh) clones the factory and the app
under `/root`, runs `uv sync --extra test` and `app.shared_check`, then `factory.py init`.
Set `APP_REPO` / `APP_DIR` for a different app. To test an unpushed branch, pass a git
bundle and the branch name: `setup-app-on-vm.sh /tmp/app.bundle my-branch`.

Check the gate before going further (~4 min on e2-standard-2):

```bash
gcloud compute ssh factory-1 $F --command 'sudo -i bash -lc "cd ~/file-to-markdown-convertor && uv run --extra test pytest tests/ -q"'
# expected: 368 passed, 2 deselected
```

## 2b. Archon console (optional, ~5 min)

```bash
gcloud compute scp bin/gcp/archon-console.sh factory-1:/tmp/ $F
gcloud compute ssh factory-1 $F --command 'sudo -i bash /tmp/archon-console.sh'
# then, on the Mac, keep this running and open http://localhost:3090
gcloud compute start-iap-tunnel factory-1 3090 --local-host-port=localhost:3090 \
  --zone me-west1-a --project me-sb-dgcp-dpoc-pocyosh-pr
```

[`bin/gcp/archon-console.sh`](../bin/gcp/archon-console.sh) serves the console from its own
copy of the pinned Archon (the pinned tree must stay clean) as systemd service
`archon-console`, sharing `~/.archon` so it shows the factory's runs. Port 3090 is open
to the IAP range only. `rebuild.sh` does this step too.

## 3. The two logins

With the tokens saved on the Mac ([one-time setup](#one-time-keep-the-tokens-on-the-mac)),
this is one command: `bin/gcp/push-logins.sh`. Interactive alternative — SSH in, then `sudo -i`:

```bash
gh auth login --hostname github.com --git-protocol https --web -s workflow
gh auth setup-git
```

Claude Code: on the Mac run `claude setup-token`, then on the VM:

```bash
echo 'export CLAUDE_CODE_OAUTH_TOKEN=<token>' >> ~/.factory-env && chmod 600 ~/.factory-env
echo 'source ~/.factory-env; export IS_SANDBOX=1' >> ~/.profile && source ~/.profile
```

## 4. Project files, check, first lap

In `/root/file-to-markdown-convertor`: fill `MISSION.md`, `harness/END-TO-END.md`,
`harness/harness.config.json` (`unit`, `required_checks: "none"` until CI exists), have the
`holdout-writer` write `.factory/holdout/HOLDOUT.md`, commit and push. Then:

```bash
python3 factory/consumer.py doctor
python3 factory/consumer.py list
python3 factory/consumer.py run archon-loop --input merge=approve
```

Scheduling stays off until a lap has been watched end to end.

## Gotchas we hit (already handled)

| Symptom | Cause | Fix |
|---|---|---|
| `serviceAccount ...-compute@developer... was not found` | The project has no default compute SA | `--no-service-account --no-scopes`; the VM needs no GCP APIs |
| `gcloud compute ssh` exits 255 | No firewall rule allows SSH | IAP-only rule + `--tunnel-through-iap` |
| `Error: do not run this installer with sudo` | Claude Code installer refuses sudo | `CLAUDE_INSTALL_ALLOW_SUDO=1` in the bootstrap call |
| No `timeout` on macOS | BSD userland | Don't use `timeout` in Mac-side commands |
| API won't start, 10 test modules fail to collect | App imported `shared/` that lived only in the private platform repo | Converter PR #2 commits the 49 `shared/` modules it uses |

## Warning: sandbox cleaner

`me-sb-*` projects are swept every Sunday by
`sandbox-cleaner@dgcp-mgmt-scheduled.iam.gserviceaccount.com`, which deletes the VM and its
firewall rule (first seen 2026-11-14, 23:22 UTC; the user confirmed the schedule as Sundays). There is no exemption, so the
factory runs on a weekly cycle:

- **Nothing lives only on the VM.** The app repo holds its committed factory files
  (`MISSION.md`, `harness/`, `.factory/`), and the Mac holds the two tokens.
- **Saturday:** `python3 factory/consumer.py halt` on the VM, so no lap is cut off mid-run.
  Open PRs survive on GitHub; the next lap picks them up.
- **Sunday after the sweep, or Monday:** one command rebuilds everything, logins included (~20 min):

  ```bash
  bin/gcp/rebuild.sh
  ```

  Then `unhalt`, and reinstall the timer if scheduling was on (`schedule install --apply`).

### One-time: keep the tokens on the Mac

```bash
mkdir -m 700 -p ~/.factory-secrets
claude setup-token                 # paste the token into the next file
(umask 077; cat > ~/.factory-secrets/claude_token)
# GitHub token with repo + workflow scope (fine-grained: just the app repo)
(umask 077; cat > ~/.factory-secrets/gh_token)
```

[`bin/gcp/push-logins.sh`](../bin/gcp/push-logins.sh) sends them to the VM over stdin
(never on a command line) and is what step 3 runs in `rebuild.sh`.

## Cost

`e2-standard-2` + 100 GB disk is about $60/month running. A stopped VM costs only the
disk (~$10). Claude tokens will cost more than the VM. With 8 GB keep `parallel` at 1.

## Tear down

```bash
gcloud compute instances delete factory-1 --project me-sb-dgcp-dpoc-pocyosh-pr --zone me-west1-a
gcloud compute firewall-rules delete allow-iap-ssh-factory allow-iap-archon-factory --project me-sb-dgcp-dpoc-pocyosh-pr
```
