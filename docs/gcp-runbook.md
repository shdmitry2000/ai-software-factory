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

Everything runs from the factory repo folder and keeps its logins and config there
(see [sign in](#one-time-sign-in-credentials-stay-in-this-folder)). The scripts load
[`bin/gcp/env.sh`](../bin/gcp/env.sh) themselves; for gcloud commands typed by hand, load
it first so they use the same in-folder login:

```bash
cd ~/code/software_factory/ai-software-factory
source bin/gcp/env.sh
PROJECT=me-sb-dgcp-dpoc-pocyosh-pr ZONE=me-west1-a VM=factory-1 TYPE=e2-standard-2 \
  bin/gcp/create-factory-vm.sh bin/bootstrap-ubuntu.sh
```

[`bin/gcp/create-factory-vm.sh`](../bin/gcp/create-factory-vm.sh) is idempotent. It:

- creates firewall rule `allow-iap-ssh-factory` (tcp:22 from `35.235.240.0/20`, tag `factory`);
- opens only SSH from the IAP range (no rule for 3090; the consoles go over SSH);
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
# then, on the Mac: opens the factory console (localhost:8080) and Archon (localhost:3090)
bin/gcp/console.sh
```

[`bin/gcp/archon-console.sh`](../bin/gcp/archon-console.sh) serves the console from its own
copy of the pinned Archon (the pinned tree must stay clean) as systemd service
`archon-console`, sharing `~/.archon` so it shows the factory's runs. No firewall rule opens 3090: `bin/gcp/console.sh`
forwards it over the same IAP SSH connection as the factory console. `rebuild.sh` does this step too.

## 3. The two logins

After signing in once ([`bin/login.sh`](#one-time-sign-in-credentials-stay-in-this-folder)),
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

`me-sb-*` projects are swept every weekend by
`sandbox-cleaner@dgcp-mgmt-scheduled.iam.gserviceaccount.com`. From its audit log
(`gcloud logging read 'protoPayload.authenticationInfo.principalEmail="sandbox-cleaner@dgcp-mgmt-scheduled.iam.gserviceaccount.com"'`):

| When (UTC) | Israel time | What it does |
|---|---|---|
| Friday ~21:00–23:00 | Fri 23:00–01:00 | disables APIs |
| Saturday 21:00 – Sunday ~00:30 | Sat 23:00 – Sun 02:30 | deletes VMs, disks, instance templates, firewall rules, subnets, GKE clusters; disables APIs (Pub/Sub, Firestore, Cloud Run, IAM, Artifact Registry, Compute, Logging, IAP…) |

Not deleted (no such actions in the log): Pub/Sub topics and subscriptions, Firestore data,
the Cloud Run service, Artifact Registry images, service accounts. They stop working while
their APIs are off and come back when re-enabled. There is no exemption, so the factory runs
on a weekly cycle:

- **Nothing lives only on the VM.** The app repo holds its committed factory files
  (`MISSION.md`, `harness/`, `.factory/`), Firestore holds history and the bridge cursor,
  and `.factory-secrets/` in this repo folder holds the two logins.
- **Friday before 21:00 UTC:** `python3 factory/consumer.py halt` on the VM, so no lap is cut
  off mid-run. Open PRs survive on GitHub; the next lap picks them up.
- **Sunday after ~01:00 UTC, or Monday:** one command re-enables the APIs, re-applies the
  writer's wiring and rebuilds the VM, Archon console, bridge and logins (~25 min):

  ```bash
  bin/gcp/rebuild.sh
  ```

  Then `unhalt`, and reinstall the timer if scheduling was on (`schedule install --apply`).

### One-time: sign in (credentials stay in this folder)

```bash
bin/login.sh            # asks only for what is missing; --check reports, --redo starts over
```

[`bin/login.sh`](../bin/login.sh) asks how to sign in and keeps everything in
`.factory-secrets/` **inside this repo folder** (mode 700, files 600), nothing in `$HOME`:

| Login | Options |
|---|---|
| Google Cloud | browser sign-in (`gcloud auth login --update-adc`), or `--no-launch-browser` on a machine without one. Config, SSH key, known hosts and docker's registry login all live in the folder (`CLOUDSDK_CONFIG`, `DOCKER_CONFIG`, a folder-local HOME for gcloud) |
| GitHub | browser sign-in (`gh auth login --web`, with `GH_CONFIG_DIR` inside the folder), or paste a fine-grained token for the app repo |
| Coding agent | Claude subscription sign-in (`claude setup-token`, `CLAUDE_CONFIG_DIR` inside the folder), paste an existing setup-token, or paste an Anthropic API key |

It checks each credential (Google Cloud by reading the project, GitHub with `gh api user`, Claude by format), refuses a folder
outside the repo or one git does not ignore, and reads tokens with `read -s`. The folder is in
`.gitignore`, and `.claude/settings.json` denies agents reading or editing it.
[`bin/gcp/push-logins.sh`](../bin/gcp/push-logins.sh) sends the credentials to the VM over
stdin (never on a command line) and is what step 3 runs in `rebuild.sh`.

## Cost

`e2-standard-2` + 100 GB disk is about $60/month running. A stopped VM costs only the
disk (~$10). Claude tokens will cost more than the VM. With 8 GB keep `parallel` at 1.

## Tear down

```bash
gcloud compute instances delete factory-1 --project me-sb-dgcp-dpoc-pocyosh-pr --zone me-west1-a
gcloud compute firewall-rules delete allow-iap-ssh-factory allow-iap-archon-factory --project me-sb-dgcp-dpoc-pocyosh-pr
```
