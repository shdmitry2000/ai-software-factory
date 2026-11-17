# Mission control: build plan in small steps

Each step is small (an hour to half a day), leaves the system working, and ends with a
check anyone can run. Do them in order; a step starts only when the previous one's
check passes. Design: [mission-control-design.md](mission-control-design.md).

Legend: ☐ to do · ◐ in progress · ☑ done

**Order (2026-11-17).** A–D done → **N** (PR-only guardrails) → **O** (compliance agent on
GKE, advisory: the owner's near-term goal) → F (dashboard) → **Q** (projects and agents)
→ **R** (fleet and scale-out) → **S** (workflow browser and messages) → I + **U** (memory
and its browser) → **T** (prompt and skill managers, independent) → E (first lap on the
converter) → G, H, J, K, L, M. Letters are names, not order; each step's check must pass
before the next starts.

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
| D1 | `runs/{id}` projection from run events | a test run shows status, start, end | ☑ |
| D2 | `runs/{id}/nodes/{node}` projection | each node shows status, agent, durations | ☑ |
| D3 | `engines/{id}` projection | heartbeat and pin visible | ☑ |
| D4 | `replay` command: rebuild projections from `events` | delete projections, replay, compare: identical | ☑ |
| D5 | Rule: out-of-order and duplicate events converge | test shuffles 100 events: same final state | ☑ |

## Stage E — First real lap (the factory itself)

On hold by the owner (2026-11-17): converter PR #3 waits until the factory is proven
(Stages N and O first). Product changes go through PRs only.

| # | Step | Done when | |
|---|---|---|---|
| E1 | `bin/login.sh` (credentials in the repo's `.factory-secrets/`); `push-logins.sh` | `gh auth status` and `claude --version` work as root on the VM | ☑ |
| E2 | Write `MISSION.md`, journeys, `harness.config.json` with you | your review done | ◐ drafted in converter PR #3, held |
| E3 | `holdout-writer` writes the holdouts; you review them | file reviewed; fence check in `doctor` passes | ◐ H-01..H-19 written, but the pinned writer pushed them to `main` (`d58edec`); redo outside the repo after N2 |
| E4 | Factory files reach the product through a PR into its configured base branch (N1) | merged | ☐ held |
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
| G3 | Ingest: GitHub webhooks → CloudEvents → bus (Cloud Run, webhook secret): the webhook adapter of the GitActivity port (Q2 polls first) | opening an issue produces `dev.factory.workitem.opened` within seconds | ☐ |
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

## Stage N — PR-only guardrails (before any agent writes to a product repo)

| # | Step | Done when | |
|---|---|---|---|
| N1 | `products/<product>.yaml`: repo, `base_branch` (undecided: the repo's current default until the owner picks), pools, compliance mode; loader + schema | a test loads the converter's file; a missing `base_branch` is an error | ☐ |
| N2 | Holdouts outside the repo: run `archon-holdouts` with `holdout=<path outside the checkout>` on a test product | the writer reports scenario ids, `git log --all` on the remote shows no new commit | ☐ |
| N3 | Holdouts backed up to a private bucket (BlobStore port, GCS adapter, contract tests) and restored by `rebuild.sh` | delete the engine copy, rebuild: the file is back, byte-identical | ☐ |
| N4 | Git-rules check (read-only): base branch requires a PR, direct pushes blocked, who can bypass | report printed per product; a repo without protection opens a problem | ☐ |
| N5 | Decide with the owner: base branch per product; the factory's own GitHub identity (App or machine user) | decisions recorded in `products/*.yaml` and the design's open questions | ☐ |
| N6 | Bridge redaction for holdout agents: metadata only (no `rawInput`, `output`, `summary`, `text`) | unit test with a synthetic holdout run: no scenario text in any published event | ☐ |

## Stage O — Compliance agent on GKE, advisory (owner's near-term goal)

Assumption: "zero effort" = running but **advisory only** (never blocks a merge), lowest
reasoning effort, cheapest model tier, daily spend cap.

| # | Step | Done when | |
|---|---|---|---|
| O1 | Agent image: pinned Archon + Claude Code + `gh` + bridge; built and pushed to Artifact Registry | `docker run <image> archon --version` and `claude --version` succeed; digest listed | ☐ |
| O2 | Check: can the pinned Archon run a workflow kept outside the product repo? | yes/no with evidence recorded in the design's open questions | ☐ |
| O3 | Compliance rules with the owner: `agents/compliance/rules.md` (ids, severity, examples) | owner reviewed the list | ☐ |
| O4 | Compliance workflow `agents/compliance/`: select PR → diff → review (small tier, low effort) → report; run on factory-1 against a test PR, report only to stdout | report JSON names rules and the reviewed head SHA; nothing written to GitHub | ☐ |
| O5 | Event type `dev.factory.compliance.reported` (schema + example) and `compliance/{product}__{pr}` projection | the O4 report appears in Firestore | ☐ |
| O6 | `bin/gke/create-cluster.sh`: Autopilot `factory-agents`, Workload Identity → `factory-agent` (publisher only); idempotent | cluster created from nothing; a test pod publishes one event | ☐ |
| O7 | Credential to the pod: Kubernetes Secret from `.factory-secrets/` over stdin (AgentAuth method recorded, never the secret) | the pod's `claude -p` test call succeeds; the secret is absent from manifests and logs | ☐ |
| O8 | Deploy `compliance-agent` (1 replica) for the converter, polling open PRs every 10 min | a new PR commit gets a report within 10 minutes; pod restart loses nothing | ☐ |
| O9 | Spend cap and effort: per-report cost in the event; daily cap stops the agent and opens a problem | a forced low cap stops it and the problem shows | ☐ |
| O10 | Agent heartbeat `dev.factory.agent.heartbeat` with location `gke:…` | its heartbeat is in Firestore every 60 s | ☐ |
| O11 | `rebuild.sh` recreates the cluster and redeploys the agent | weekend drill: agent back and reporting, no duplicate reports for already-reviewed heads | ☐ |
| O12 | Advisory PR comment (one comment, edited in place, marked advisory) — after N5's identity decision | comment appears on a test PR; merge is unaffected | ☐ |

## Stage Q — Projects and agents status (on the F dashboard)

| # | Step | Done when | |
|---|---|---|---|
| Q1 | `products` projection from `products/*.yaml` (`dev.factory.product.registered`) | both the converter and the test product listed | ☐ |
| Q2 | GitActivity port + contract tests; GitHub **polling** adapter (ETag, 60 s) | contract tests pass against a test repo | ☐ |
| Q3 | Poller publishes PR, push, check and issue events for every product | opening a PR produces `dev.factory.pr.opened` within 2 minutes | ☐ |
| Q4 | Product rollups: open PRs, last lap, problems, compliance findings, `git_seen_at` | rollup matches GitHub and Firestore by hand count | ☐ |
| Q5 | `agents` projection from agent heartbeats (VM and GKE alike) | factory-1 and the compliance pod both listed with location and state | ☐ |
| Q6 | Projects view | every product with its rollups; stale git data marked | ☐ |
| Q7 | Agents view | each agent's location, state, current work, model and auth method | ☐ |

## Stage R — Agent fleet and automatic scale-out

| # | Step | Done when | |
|---|---|---|---|
| R1 | AgentRuntime port + contract tests (deploy, scale, list, drain, delete) with an in-memory fake | contract tests pass on the fake | ☐ |
| R2 | GKE adapter | contract tests pass on `factory-agents` | ☐ |
| R3 | GCE adapter wrapping `create-factory-vm.sh` | contract tests pass with a throwaway VM | ☐ |
| R4 | `fleet.yaml` schema + loader (pools: kind, runtime, image, products, min/max, scale_on, model, mode) | the compliance and builders pools load | ☐ |
| R5 | Reconciler as a Cloud Run job (every minute): desired vs listed | delete the compliance pod: it is back within 2 minutes | ☐ |
| R6 | Demand-based scaling (`pending_prs`, later queue depth) within min..max | 3 waiting PRs scale compliance 1 → 3, then back to 1 | ☐ |
| R7 | `dev.factory.command.scale` + "Add agent" / "Drain" on the Agents view | the button adds a pod that appears in Agents | ☐ |

## Stage S — Workflow browser and agent messages

| # | Step | Done when | |
|---|---|---|---|
| S1 | Capture spike on a two-agent test workflow: answer the open questions (subagent tool attribution, all assistant turns or last only, truncation size, node's own prompt) | answers with evidence in the design | ☐ |
| S2 | Event types `agent.dispatched`, `agent.replied`, `agent.message`, `tool.called`, `tool.returned` (schemas + examples) | examples validate in CI | ☐ |
| S3 | Archon adapter maps `provider_event` → those types (pure function, fixture from S1) | unit tests pass; no event over 16 KB | ☐ |
| S4 | Redaction: holdout agents metadata-only (N6), secret scanner on bodies | test with a planted token and a synthetic holdout run: neither published | ☐ |
| S5 | Large bodies to the BlobStore with a 2 KB preview + hash | a 19 KB dispatch prompt is readable from its blob | ☐ |
| S6 | `runs/{run}/messages/{seq}` projection (from, to, kind, preview, blob ref) | a lap's messages in order, threaded by `parentToolCallId` | ☐ |
| S7 | Workflow catalog `workflows/{name}@{digest}` from captured sources and `consumer.py list` | every pinned workflow and the compliance workflow listed with graphs | ☐ |
| S8 | Workflows view: workflow → runs → graph → per-node conversation | a lap's dispatch prompts and replies readable per node | ☐ |

## Stage U — Memories browser (after I1–I2)

| # | Step | Done when | |
|---|---|---|---|
| U1 | `memory_index` projection (metadata only) from the MemoryStore | the converter's mission, decisions, conventions listed with versions | ☐ |
| U2 | Memories view: read, history, diff between versions, source link (PR or run) | a document's two versions diff correctly | ☐ |
| U3 | "Propose a change" → pull request through the CodeHost port (git adapter) | a PR opens against the product's base branch; nothing pushed to it | ☐ |
| U4 | Holdouts as ids, protected rule and verdict history only | no holdout text in Firestore or the page | ☐ |
| U5 | Agent-written notes and lessons shown with provenance (run id) | a lesson links to the run that wrote it | ☐ |

## Stage T — Prompt and skill managers (independent services)

Design: [prompt-skill-manager-design.md](prompt-skill-manager-design.md).

| # | Step | Done when | |
|---|---|---|---|
| T1 | OpenAPI spec + CloudEvents types + PromptRegistry / SkillRegistry ports | spec lints; ports importable | ☐ |
| T2 | Contract tests + in-memory adapters (versions immutable, labels per environment, locks) | contract tests pass | ☐ |
| T3 | Git-repo adapters (prompts as files, skills as folders, labels file) | same contract tests pass on a test repo | ☐ |
| T4 | Service on Cloud Run with Firestore + Cloud Storage, OIDC for people, engine tokens | contract tests pass against the deployed service | ☐ |
| T5 | Locks: `POST /v1/locks`, fetch by hash, `dev.prompts.lock.pinned` | two laps with the same lock fetch identical bytes | ☐ |
| T6 | Lap materializer: skills and subagents into a lap-scoped `CLAUDE_CONFIG_DIR`; check the repo's `.claude/` hook still runs | a lap uses a managed skill; the holdout fence still refuses; repo stays clean | ☐ |
| T7 | Move the compliance agent's prompt and rules into the manager | a label move changes the next report's prompt version, not the current one | ☐ |
| T8 | Read-only import of the pinned Archon commands (label `archon-pin-<sha>`) | the pinned prompts are browsable and diffable | ☐ |
| T9 | Promotion rule: `production` needs a passing EvalLink and a second approver | moving it without them is refused | ☐ |
| T10 | Console: each run shows its lock and links to the versions | a run page lists its prompt and skill versions | ☐ |

## Rules for every step

- One pull request per step, with its check in the description.
- New code talks only to ports; an adapter is added with its contract tests passing.
- Nothing is lost on the weekend sweep: state lives in the bus, the store, GitHub, Artifact Registry, Cloud Storage, or the repo's `.factory-secrets/`.
- Product repos change through pull requests only; nothing pushes to a shared branch. A PR's base branch comes from `products/<product>.yaml`, never from an assumption.
