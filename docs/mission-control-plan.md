# Mission control: build plan in small steps

Each step is small (an hour to half a day), leaves the system working, and ends with a
check anyone can run. Do them in order; a step starts only when the previous one's
check passes. Design: [mission-control-design.md](mission-control-design.md).

Legend: ☐ to do · ◐ in progress · ☑ done

## Stage 0 — Ground already laid

| # | Step | Done when | |
|---|---|---|---|
| 0.1 | Factory VM + rebuild scripts ([gcp-runbook.md](gcp-runbook.md)) | `bin/gcp/rebuild.sh` brings up `factory-1` | ☑ |
| 0.2 | Converter runs from a plain clone (`shared/`, `/build-id`) | converter PR #2 merged; 368 tests pass on the VM | ☑ |
| 0.3 | GCP resources: topics, DLQ, service accounts, registry, Firestore | `mission-control/infra/setup.sh` reruns clean | ☑ |

## Stage A — Contracts before code

| # | Step | Done when | |
|---|---|---|---|
| A1 | Write the event type list (`dev.factory.<entity>.<verb>`) | `schema/types.md` lists every type with one line each | ☑ |
| A2 | JSON Schema for the CloudEvents envelope + factory extensions | 3 hand-written example events validate | ☑ |
| A3 | JSON Schema per event type's `data` (P0 types now; each later stage adds the types it publishes) | every example in `schema/examples/` validates in CI | ☑ |
| A4 | Python package skeleton `mission_control` with `ports.py` (EventBus, StateStore, WorkTracker, CodeHost, MemoryStore, EngineAdapter, Secrets, Notifier) | `python -c "import mission_control.ports"` works | ☑ |
| A5 | Contract test suite skeleton, parametrised by adapter | `pytest contract_tests` runs (no adapters yet, all skipped) | ☑ |
| A6 | In-memory adapters for EventBus and StateStore | their contract tests pass | ☑ |
| A7 | `mission-control.yaml` loader that picks adapters by name | a test swaps `memory` for `memory2` with no code change | ☑ |

## Stage B — Bus and store (P0)

| # | Step | Done when | |
|---|---|---|---|
| B1 | Pub/Sub EventBus adapter: publish with ordering key | contract test passes against a throwaway test topic | ☑ |
| B2 | Pub/Sub EventBus adapter: pull subscribe + ack | publish → receive round trip test passes | ☑ |
| B3 | Firestore StateStore adapter: idempotent upsert of `events/{id}` | publishing the same event twice leaves one document | ☑ |
| B4 | Writer as a local HTTP service: Pub/Sub push body → validate → store | `curl` of an example event creates the document | ☑ |
| B5 | Writer container image pushed to Artifact Registry | image tag listed in the registry | ☑ |
| B6 | Writer deployed to Cloud Run, private, as `factory-writer` | `gcloud run services describe` shows it ready | ☑ |
| B7 | Push subscription `factory-events-writer` → writer, with DLQ after 10 tries | a published example appears in Firestore; a malformed one lands in the DLQ | ☑ |

## Stage C — Engine side on the VM (P0)

| # | Step | Done when | |
|---|---|---|---|
| C1 | Recreate/attach the VM with service account `factory-vm`; add to `create-factory-vm.sh` | `gcloud pubsub topics publish factory-events` works from the VM | ☑ |
| C2 | IAP firewall for port 3090 | rule exists; added to the create script | ☑ |
| C3 | `archon serve` as a systemd service on the VM | Archon console opens on the Mac through `gcloud compute start-iap-tunnel` | ☑ |
| C4 | Find Archon's DB file and schema on the VM; read-only dump script | the script prints the last 10 events | ☑ |
| C5 | Archon EngineAdapter: DB rows → CloudEvents (pure function); graph edges from the run's captured workflow source (`metadata.workflow_source.root`, `depends_on`) | unit tests on fixture rows; output validates against A3 schemas | ☑ |
| C6 | Bridge loop: tail with cursor, publish through the EventBus port | a bash-only test workflow's events appear in Firestore | ☑ |
| C7 | Bridge cursor kept in `engines/{engine}` | kill and restart the bridge: no gaps, no duplicates | ☑ |
| C8 | Heartbeat event every 60 s | heartbeat events arrive every 60 s (`engines/{id}` fields come with D3) | ☑ |
| C9 | Bridge as a systemd service; added to `rebuild.sh` | after a rebuild the bridge resumes from its cursor | ☑ |

## Stage D — Projections (P0 done here)

| # | Step | Done when | |
|---|---|---|---|
| D1 | `runs/{id}` projection from run events | a test run shows status, start, end | ☐ |
| D2 | `runs/{id}/nodes/{node}` projection | each node shows status, agent, durations | ☐ |
| D3 | `engines/{id}` projection | heartbeat and pin visible | ☐ |
| D4 | `replay` command: rebuild projections from `events` | delete projections, replay, compare: identical | ☐ |
| D5 | Rule: out-of-order and duplicate events converge | test shuffles 100 events: same final state | ☐ |

## Stage E — First real lap (the factory itself)

| # | Step | Done when | |
|---|---|---|---|
| E1 | Tokens in `~/.factory-secrets`; `push-logins.sh` | `gh auth status` and `claude --version` work as root on the VM | ☐ |
| E2 | Write `MISSION.md`, journeys, `harness.config.json` with you | your review done | ☐ |
| E3 | `holdout-writer` writes `HOLDOUT.md`; you review it | file reviewed; fence check in `doctor` passes | ☐ |
| E4 | Commit and push the factory files (PR) | merged | ☐ |
| E5 | `consumer.py doctor` and `list` | no failures | ☐ |
| E6 | One lap: `run archon-loop --input merge=approve` | the lap is visible in Archon's console **and** in Firestore `runs` | ☐ |

## Stage F — Dashboard v1, read-only (P1)

| # | Step | Done when | |
|---|---|---|---|
| F1 | Web skeleton, Google sign-in, Firestore rules read-only for the org | you can sign in and see an empty page; a stranger cannot | ☐ |
| F2 | Engines view | factory-1 heartbeat live | ☐ |
| F3 | History: list of runs | the lap from E6 is listed | ☐ |
| F4 | History: retry chains grouped by work item, status filters | a rerun shows under its first attempt | ☐ |
| F5 | Problem rule: failed step opens, later success resolves | forced failure opens a problem; rerun resolves it | ☐ |
| F6 | Problems view | the problem from F5 shows and clears | ☐ |
| F7 | Time rules (stuck run, silent engine, waiting on human) via Cloud Scheduler every 5 min | stopping the bridge opens "engine silent" within 5 min | ☐ |
| F8 | Deploy the dashboard to Cloud Run | reachable at its URL behind sign-in | ☐ |

## Stage G — Work items through the tracker and code host ports (P2)

| # | Step | Done when | |
|---|---|---|---|
| G1 | WorkTracker + CodeHost ports' contract tests | run against an in-memory fake | ☐ |
| G2 | GitHub adapter (read): issues, comments/claims, PRs, checks, reviews, merges | contract tests pass against a test repo | ☐ |
| G3 | Ingest: GitHub webhooks → CloudEvents → bus (Cloud Run, webhook secret) | opening an issue produces `dev.factory.workitem.opened` | ☐ |
| G4 | `work_items` projection (stage, laps, PR, checks, gate verdict) | the E6 issue shows its whole path | ☐ |
| G5 | Board view | cards move between columns live | ☐ |
| G6 | Slack Notifier adapter | a new high problem posts to the channel | ☐ |

## Stage H — Live console (P2)

| # | Step | Done when | |
|---|---|---|---|
| H1 | Publish the workflow graph (nodes + edges) at `workflow.started` | `runs/{id}/edges` filled for a run | ☐ |
| H2 | Console graph: nodes coloured by live status, parallel branches side by side | a lap's fan-out shows 3 running nodes at once | ☐ |
| H3 | Handoff edges (artifact or output passed) | clicking an edge shows what was handed over | ☐ |
| H4 | Tool-activity rollup per node + "agents working now" | live tool line updates during a lap | ☐ |
| H5 | OpenTelemetry export (`traceparent` → spans) | the same lap opens as a trace in Cloud Trace | ☐ |

## Stage I — Memory port (git files ⇄ database)

| # | Step | Done when | |
|---|---|---|---|
| I1 | MemoryStore contract tests (versioning, scope, search) | pass against an in-memory fake | ☐ |
| I2 | Git-files adapter (reads `MISSION.md`, `DECISIONS.md`, `engineering.md`) | contract tests pass on the converter repo | ☐ |
| I3 | Firestore memory adapter | same contract tests pass | ☐ |
| I4 | `dev.factory.memory.changed` events on every put | editing a document publishes one event | ☐ |
| I5 | Materializer: memory → files in a lap checkout at lap start; holdouts only into the verifier's sandbox | a lap reads mission text that exists only in the DB | ☐ |
| I6 | Cutover tool: copy git → DB, diff, flip `mission-control.yaml` | flip and flip back with no diff | ☐ |

## Stage J — Tracker swap (Jira or any other)

| # | Step | Done when | |
|---|---|---|---|
| J1 | Jira adapter (read) | WorkTracker contract tests pass on a sandbox Jira project | ☐ |
| J2 | Jira adapter (write: comment, label, claim) | contract tests pass | ☐ |
| J3 | Mirror adapter: tracker item ⇄ GitHub issue for the engine | a Jira ticket gets a mirrored GitHub issue; status syncs both ways | ☐ |
| J4 | Dual-run: GitHub + Jira adapters together | Board shows both without duplicates | ☐ |
| J5 | Cutover drill on a test product | a lap runs end to end from a Jira ticket | ☐ |

## Stage K — Distributed engines (P3)

| # | Step | Done when | |
|---|---|---|---|
| K1 | Gateway: engine tokens; events in → bus | an engine token can post an event; a wrong token cannot | ☐ |
| K2 | Gateway: commands out (long poll per engine) | a test command reaches the right engine only | ☐ |
| K3 | Bridge talks to the gateway instead of Pub/Sub (adapter swap) | factory-1 works with no GCP service account | ☐ |
| K4 | `join.sh`: toolchain install (Linux, then macOS) | a fresh VM gets the tools from one command | ☐ |
| K5 | Docker sandbox: one container per lap, worktree-only mount | a lap runs inside a container; host files untouched | ☐ |
| K6 | Registration → `pending` engine on the dashboard | the new machine appears as pending | ☐ |
| K7 | GitHub login prompt on dashboard and terminal (device code or token) | GitHub login finishes without SSH | ☐ |
| K7a | AgentAuth port + contract tests (method list, store, test call, fallback order) | pass against a fake provider | ☐ |
| K7b | Claude Code adapter: interactive sign-in / device code | engine signs in from the dashboard link | ☐ |
| K7c | Claude Code adapter: OAuth token and API key | both pass the test call | ☐ |
| K7d | Claude Code adapter: Vertex AI / Bedrock identity | test call succeeds with no pasted secret | ☐ |
| K7e | Claude Code adapter: org gateway (base URL + token) | test call through the gateway succeeds | ☐ |
| K7f | Codex adapter (sign-in, device code, API key) | same contract tests pass | ☐ |
| K7g | Fallback order (e.g. subscription → API key on usage limit) | a forced limit switches method and is shown on Engines | ☐ |
| K8 | Approve → engine token issued → factory installed → `live` | the second engine runs a lap | ☐ |
| K9 | Engine actions: pause, drain, revoke | revoke stops its next event at the gateway | ☐ |

## Stage L — Coordinator and Flow (P4)

| # | Step | Done when | |
|---|---|---|---|
| L1 | `queue/{product}` projection: workable items, dependencies, claims | queue order matches the backlog | ☐ |
| L2 | Reasons on each queue entry | each entry says why it is where it is | ☐ |
| L3 | `assign` command; engines honour it (still claim on the tracker) | work lands on the engine with a free slot | ☐ |
| L4 | Reassign when an engine goes silent | stop one engine: its item moves within the timeout | ☐ |
| L5 | Flow view: done → doing → next | matches the canvas design | ☐ |

## Stage M — Controls from the dashboard

| # | Step | Done when | |
|---|---|---|---|
| M1 | Halt / unhalt via `factory-commands` | the button halts the engine; audit event recorded | ☐ |
| M2 | Approve a waiting merge | approval reaches Archon; PR merges | ☐ |
| M3 | Rerun a failed run | a new attempt starts, chained in History | ☐ |

## Rules for every step

- One pull request per step, with its check in the description.
- New code talks only to ports; an adapter is added with its contract tests passing.
- Nothing is lost on the Sunday sweep: state lives in the bus, the store, GitHub, or the Mac's secrets folder.
