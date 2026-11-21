# Mission control: design

A live console and history for the factory: what is running, what finished, what
failed or was rerun, where each piece of work is, and how the agents handed work to
each other. Built on GCP Pub/Sub and Firestore, outside the factory VM, so history
survives the weekly sandbox sweep.

Architecture overview: [architecture.md](architecture.md). Status: design agreed 2026-11-15; **P0 done 2026-11-15** (Stages A–D of the plan): factory-1's Archon runs reach Firestore projections live. Pinned Archon: `1cfb8fc5`.
2026-11-17: added [git rules](#git-rules-prs-only), [projects and agents](#projects-and-agents-status),
[agent fleet](#agent-fleet-and-automatic-scale-out), [GKE agents and the compliance agent](#gke-agents-and-the-compliance-agent),
[workflow browser](#workflow-browser-and-agent-messages), [memories browser](#memories-browser), and the
independent [prompt and skill managers](prompt-skill-manager-design.md).

## Goals

1. **Console.** See every active run as it happens: its flow as a graph (parallel
   branches included), the step each branch is on, which agent and model is working,
   its live tool activity, and the handoffs between agents.
2. **History and monitoring.** Every run and task as finished / failed / rerun /
   cancelled / abandoned, with the retry chain grouped, durations, cost, and an open
   **Problems** list that tells someone what needs a human.
3. **Not sequential.** Flows will gain more junctions (fan-out, parallel review,
   joins). Nothing in the model may assume a run is a list of steps.
4. **Many engines.** More than one factory engine (VMs, products) publish to and
   consume from the same bus.

## Non-goals

- **No change to Archon.** Everything reads what Archon already writes. Engine
  changes belong upstream in Archon's SDLC pack.
- **Not a replacement for Archon's own console.** `archon serve` keeps the deep view of
  one run (event log, artifacts, approvals). Mission control is the factory-wide view
  and links into it.
- **No judging.** It reports gate verdicts; it never decides one.

## What Archon already records

| Data | Where (Archon DB, SQLite on the VM by default) |
|---|---|
| Runs | `remote_agent_workflow_runs`: workflow, status (`pending running completed failed cancelled paused`), outcome, metadata, parent conversation, start/end, last activity |
| Events | `remote_agent_workflow_events`: `workflow_started/completed/failed/cancelled`, `node_started/completed/failed/skipped/suspended`, `node_skipped_prior_success` (a rerun skipping work already done), `loop_iteration_*`, `approval_pending`, `workflow_artifact`, `provider_event` (agent tool activity), `container_lifecycle` |
| Agent sessions | `remote_agent_workflow_node_sessions`: provider session per node, across reruns |
| Agent activity (checked 2026-11-17 on factory-1, `archon-holdouts` run) | `provider_event` rows per AI node, `data.event.type` one of: `tool_call` (`name`, `rawInput`, `toolCallId`; an `Agent` call's `rawInput` holds `subagent_type`, `description` and the full dispatch `prompt`), `tool_call_update` (`output`, `status`, sometimes `outputTruncated`), `subtask` (a subagent's lifecycle: `taskId`, `parentToolCallId`, `status`, `lastToolName`, `usage`, and its reply as `summary`), `agent_message_chunk` (`text`), `state_update`. Largest stored `rawInput` 19.7 KB, `output` 18.6 KB. `node_completed.node_output` holds the node's final text. `remote_agent_messages` is empty: it serves chat platforms, not workflows |

Archon's engine is already a DAG executor (fan-out, parallel nodes, sub-runs). What
is missing is the cross-run, cross-source story: issue → laps → PR → checks → review →
merge gate → deploy, which today is split between the Archon DB, GitHub and the
factory's own files (`.factory/ledger.jsonl`, `.factory/gate.json`,
`.factory/schedule.log`, `holdout-exposures.jsonl`).

## Architecture

```
 FACTORY VM (wiped Sundays)                 GCP (persists)
┌─────────────────────────┐
│ Archon (SQLite)         │
│   runs, events ─────────┼─▶ archon-bridge ─┐
│ factory files ──────────┼─▶ (same process) │
│ heartbeat ──────────────┼──────────────────┤
└─────────────────────────┘                  ▼
                                   ┌───────────────────┐     ┌───────────────────┐
 GitHub ── webhooks ─▶ gh-ingest ─▶│ Pub/Sub topic     │────▶│ writer (Cloud Run)│
          (Cloud Run)              │ factory-events    │push │ projections       │
                                   │ ordering: run_id  │     └─────────┬─────────┘
                                   └──┬──────────┬─────┘               ▼
                                      │          │           ┌───────────────────┐
                              alerts ◀┘          └▶ other    │ Firestore         │
                              (Slack)              engines   │ runs, nodes,      │
                                                             │ events, work items│
 factory-commands topic (P3): halt / approve / rerun         │ problems, engines │
   ─▶ command agent on each VM                               └─────────┬─────────┘
                                                                       │ live listeners
                                                             ┌─────────▼─────────┐
                                                             │ Dashboard (web)   │
                                                             └───────────────────┘
```

### The event envelope: CloudEvents 1.0

Every source publishes a [CloudEvents 1.0](https://cloudevents.io) event (JSON
structured mode) to `factory-events`. CloudEvents is the CNCF standard that Pub/Sub,
Eventarc, Kafka, NATS and Knative all speak, so changing the bus never changes the
message:

```json
{
  "specversion": "1.0",
  "id": "archon:7f3c…",
  "source": "factory://factory-1/archon",
  "type": "dev.factory.node.started",
  "subject": "run/8f3c21/node/review",
  "time": "2026-11-15T12:00:03.120Z",
  "datacontenttype": "application/json",
  "dataschema": "https://…/schema/factory-event.v1.json",
  "factoryproduct": "file-to-markdown-convertor",
  "factoryengine": "factory-1",
  "factoryrun": "8f3c21",
  "factoryparentrun": "",
  "factoryworkitem": "tracker:github/shdmitry2000/file-to-markdown-convertor#12",
  "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
  "data": {
    "workflow": "archon-loop", "node": "review", "attempt": 1, "status": "running",
    "agent": {"provider": "claude", "model": "claude-sonnet-5-5", "tier": "medium"},
    "sandbox": "container:lap-8f3c"
  }
}
```

- **`id` is deterministic** (the Archon event's id, the tracker's delivery id, a
  hash of the ledger line). It is also the store's document id, so a replay or a
  duplicate delivery writes the same document: delivery is at-least-once and harmless.
- **`type` names are a closed, versioned list** (`dev.factory.<entity>.<verb>`) kept in
  `schema/`; the JSON Schema per type is the contract every adapter is tested against.
- **`traceparent`** (W3C Trace Context) makes each run an OpenTelemetry trace and each
  node a span, so any OTLP backend can show the same flow.
- **Work items are tracker-neutral ids** (`tracker:<adapter>/<key>`), never a GitHub URL,
  so the model does not change when the tracker does.
- **Ordering key = `factoryrun`**, so one run's events arrive in order; different runs
  flow in parallel.
- **`data` is capped at 16 KB.** Larger payloads (artifacts, long agent output) are
  truncated with a pointer to Archon's artifact. Tool-activity events (`provider_event`)
  are rolled up per node (count, last 20) instead of one document each, to keep
  Firestore writes and cost bounded.
- **Redaction** happens in the bridge before publishing: token-shaped strings
  and anything under `.factory/holdout/` are dropped, so the holdout fence holds on
  the bus too. Path filtering is not enough: the holdout agents' own tool calls and
  replies carry scenario text in `rawInput`, `output` and `summary` (see
  [what Archon records](#what-archon-already-records)). So for any node or subtask
  belonging to a holdout agent (`holdout-writer`, `holdout-verifier`, the
  `archon-holdouts` workflow) the bridge publishes **metadata only** (type, tool name,
  sizes, status, verdict) and never content.

### Sources

| Publisher | Runs on | Reads | Publishes |
|---|---|---|---|
| **archon-bridge** | each factory VM, systemd service | Archon DB events/runs (cursor on `created_at` + seen ids, like Archon's own dashboard poller); `.factory/*` logs; `consumer.py status --json` | Archon events, run snapshots, factory ledger/gate/schedule events, `engine_heartbeat` every 60 s |
| **gh-ingest** | Cloud Run, public HTTPS, webhook secret | GitHub webhooks: issues, issue comments (claims), PRs, reviews, check runs, merges | `issue_*`, `claim`, `pr_*`, `check_*`, `review_*`, `merged` |

The bridge stores its cursor in Firestore (`engines/{engine}`), so a VM rebuilt after
the Sunday sweep resumes where the old one stopped.

### Topics and subscriptions

| Name | Type | Purpose |
|---|---|---|
| `factory-events` | topic, message ordering | every event from every source |
| `factory-events-writer` | push subscription → writer | builds the Firestore projections |
| `factory-events-alerts` | push subscription → alerts (P2) | Slack on new problems |
| `factory-events-engine-<name>` | pull subscription (P3) | engines that react to each other |
| `factory-events-dlq` | topic | undeliverable after 10 attempts; shown in Problems |
| `factory-commands` | topic (P3) | dashboard → engines: halt, unhalt, approve, rerun |

### Firestore data model

| Collection | Document | Holds |
|---|---|---|
| `events` | `{event_id}` | the envelope, as received; TTL 90 days |
| `runs` | `{run_id}` | workflow, product, work item, status, outcome, attempt, parent run, started/ended, current nodes, failure summary, cost |
| `runs/{run_id}/nodes` | `{node_id}` | status, agent/model/tier, started/ended, attempts, tool-activity rollup, artifacts |
| `runs/{run_id}/edges` | `{from}->{to}` | the graph: from the workflow definition at `workflow_started`, plus a `handoff` record (artifact or output passed) when the downstream node starts |
| `work_items` | `{owner}__{repo}__{n}` | stage (backlog → claimed → building → app check → review → gate → merged → deployed), laps (run ids), PR, checks, gate verdict and reason, blockers |
| `problems` | `{id}` | kind, severity, open/resolved, first/last seen, links to run/PR |
| `engines` | `{engine}` | last heartbeat, halted/paused, bridge cursor, Archon pin, parallel |
| `products` | `{product}` | repo, mission summary, counts for the overview |

The writer updates `runs`, `nodes`, `edges`, `work_items` and `problems` from each
event inside a transaction keyed on the document, so out-of-order or repeated events
converge on the same state.

### How the model handles junctions and reruns

- **Graph, not list:** a run is nodes plus edges. Parallel branches are several
  `running` nodes at once; a join is a node with several incoming edges; fan-out
  children are runs with `parent_run_id`, drawn nested inside the parent node.
- **Reruns:** a resumed run keeps its `run_id` and increments `attempt`; nodes skipped
  as `node_skipped_prior_success` are drawn as "reused". A new lap on the same work
  item is a new run linked from `work_items.laps`, so the History view groups the chain.
- **Terminal states** come from Archon (`completed`, `failed`, `cancelled`) plus two
  the factory adds: `abandoned` (the scheduler gave up on a silent lap) and
  `lost` (the VM was swept while the run was live; set when a heartbeat stops).

### Problems: detection rules

| Rule | Opens when | Resolves when |
|---|---|---|
| Failed step | `node_failed` / `workflow_failed` | a later attempt of that node completes |
| Stuck run | `running` with no event for 30 min | any new event, or the run ends |
| Waiting on a human | `approval_pending` older than 2 h | approval resolved |
| PR held | merge gate holds a PR | PR merged or closed |
| Holdout exposure | a line in `holdout-exposures.jsonl` | a human marks it reviewed |
| Engine silent | no heartbeat for 3 min | heartbeat returns |
| Undeliverable events | a message in `factory-events-dlq` | replayed |

Time-based rules (stuck, silent, waiting) are evaluated by a Cloud Scheduler job
calling the writer every 5 minutes.

## Dashboard

**Changed 2026-11-17:** the console runs **on the main node only** (factory-1), on a
unix socket reached through an IAP SSH tunnel; its server reads Firestore with the main
node's identity and pushes live updates over SSE; every action is a command event.
No public URL, no browser access to Firestore. Plan, screens and wireframes:
[console-ui-plan.md](console-ui-plan.md); the whole system: [architecture.md](architecture.md).

| View | Answers | Reads |
|---|---|---|
| **Console** | What is running right now, on which step, by which agent, talking to whom | `runs` where status in (running, paused), their `nodes`, `edges`, live tool rollup |
| **Board** | Where each work item is, and what blocks it | `work_items` by stage |
| **History** | What finished / failed / was rerun, how long, how much | `runs` by time, grouped by work item; filters: product, workflow, status, engine |
| **Problems** | What needs a human now | `problems` where open, by severity |
| **Engines** | Is each factory alive, halted, paused, on which pin | `engines` |
| **Projects** | Every product at a glance: open PRs, last lap, problems, compliance findings, freshness of its git data | `products`, `work_items`, `problems`, `compliance` |
| **Agents** | Every agent instance: where it runs (VM, GKE pod, laptop), pool, state, current work, model, sign-in method; add or remove agents | `agents`, `pools` |
| **Workflows** | Every workflow definition, its runs, and the messages its agents exchanged | `workflows`, `runs`, `runs/{id}/messages` |
| **Memories** | Each product's mission, decisions, conventions and lessons, with history and diffs | `memory_index` |

Each run links to its page in Archon's console for the deep view.

## Interfaces: ports and adapters

Every outside system sits behind a **port** (an interface owned by mission control) and
is reached through an **adapter** chosen in configuration. Core code imports ports
only. Switching Jira for another tracker, Firestore for Postgres, or git-file memory for
database memory is a new adapter plus a config change, never a change to the core.

| Port | Contract (standard where one exists) | Adapter today | Swap to, e.g. |
|---|---|---|---|
| **EventBus** | publish / subscribe of CloudEvents 1.0, ordering key, ack, DLQ | GCP Pub/Sub | NATS, Kafka, Redis Streams, Postgres NOTIFY |
| **StateStore** | typed repositories per collection (`runs`, `nodes`, `work_items`…), idempotent upsert by id, query, live listen | Firestore | Postgres, MongoDB |
| **WorkTracker** | `WorkItem` schema; list, get, comment, label, claim / release, dependencies | GitHub Issues | Jira, Linear, Azure Boards, any other tracker |
| **CodeHost** | branches, pull requests, checks, reviews, merge, protected paths | GitHub | GitLab, Bitbucket, Azure Repos |
| **MemoryStore** | product knowledge (mission, decisions, conventions, lessons, holdouts) as versioned documents: get, put, list, search, history; per-document access scope | git files in the app repo | Postgres + pgvector, Firestore, an MCP memory server |
| **EngineAdapter** | runs, nodes, events out; start, cancel, approve in | Archon (read its DB) | another workflow engine |
| **Sandbox** | OCI container spec: image, mounts, limits, network policy | Docker | Podman, gVisor, Kubernetes pod |
| **Secrets** | get / put by name, per engine | mode-600 file on the engine | GCP Secret Manager, Vault |
| **AgentAuth** | sign-in methods per coding agent: interactive, device code, OAuth token, API key, cloud identity, gateway; test call | Claude Code (all methods) | Codex, Pi, others |
| **Notifier** | send(problem or event, audience) | Slack | Teams, email |
| **Identity** | OIDC sign-in, roles (owner, viewer, engine) | Google sign-in | Okta, Entra ID, any OIDC |
| **Telemetry** | OpenTelemetry traces and metrics over OTLP | Cloud Trace | Grafana Tempo, Honeycomb, any OTLP |
| **Gateway API** | OpenAPI 3.1 (HTTP), AsyncAPI 3.0 (the bus) | Cloud Run | anything that serves the spec |
| **AgentRuntime** | pools of agent instances: deploy(spec), scale(pool, n), list, drain, delete; spec is an OCI image + env + resources | GKE (Kubernetes API) | GCE VMs (`create-factory-vm.sh`), local Docker, Cloud Run jobs, any Kubernetes |
| **BlobStore** | put/get by content hash, signed read URL, TTL | Cloud Storage | S3, Azure Blob, MinIO, local disk |
| **GitActivity** | product repo changes as events: PRs, pushes, checks, issues | GitHub polling (no public endpoint) | GitHub webhooks (`gh-ingest`), GitLab |
| **PromptRegistry**, **SkillRegistry** | see [prompt-skill-manager-design.md](prompt-skill-manager-design.md); mission control is only a client | the factory's own service | Langfuse, MLflow, a git repo, OCI artifacts |

### What makes a swap quick

1. **One contract test suite per port.** Every adapter passes the same tests
   (`contract_tests/<port>/`), so "the new adapter works" is a test result, not an opinion.
2. **Event-sourced state.** The store is a projection of the event log, so moving to a
   new store is: point a new adapter at it, `replay` the events, compare, flip config.
3. **Dual-run cutover.** Any port can run two adapters at once (write both, read one)
   for a cutover period, then drop the old one.
4. **Neutral ids everywhere** (`tracker:jira/FAC-12`, `codehost:github/owner/repo#16`),
   so history from before a swap still resolves.

### Where the engine limits a swap (honestly)

The factory engine is Archon's shared workflows, which mission control does not change:

| Swap | Mission control | The engine (Archon workflows) | How |
|---|---|---|---|
| Tracker → Jira or another | adapter, done | workflows read and claim **GitHub issues** with `gh` | a **mirror adapter** keeps a GitHub issue per tracker item (create, sync status and comments both ways); the engine is unchanged. Long term: an upstream tracker port in Archon |
| Code host → GitLab | adapter, done | workflows open PRs and merge with `gh` | needs Archon support; until then the code host stays GitHub |
| Memory → database | adapter, done | workflows read `MISSION.md`, `engineering.md`, holdouts **as files in the checkout** | a **materializer** writes the documents into each lap's checkout before it starts (holdouts only into the verifier's sandbox), and publishes `dev.factory.memory.changed` events so engines pick up edits without a git round trip |
| Bus, store, secrets, notifier, identity, telemetry | adapter, done | not involved | config change |

## Distributed engines

An engine is one machine running the factory: Archon, the coding agent, a sandbox for
the agents' work, and the bridge. Any machine can join: a GCE VM, a laptop, an
on-prem box.

### Joining

From the dashboard, **Add engine** shows a one-line command with a short-lived join
token (single use, 30 min):

```bash
curl -fsSL https://<mission-control>/join.sh | bash -s -- --token <join-token>
```

The join agent then, on that machine:

1. Checks the OS and installs the toolchain (the same steps as `bin/bootstrap-ubuntu.sh`;
   Homebrew on macOS).
2. Installs the **sandbox** the agents run in: Docker, with one container per lap and
   no host mounts beyond the lap's worktree. Where Docker is not allowed it falls back to
   Archon's worktree isolation and the dashboard marks the engine `unsandboxed`.
3. Registers with mission control: engine name, machine, location, CPU/RAM, sandbox
   kind. The engine appears on the **Engines** screen as `pending`.
4. **Asks for what it cannot get alone**, shown on the dashboard and in the terminal:
   - GitHub: a device-flow code (`gh auth login --web`) or a pasted fine-grained token;
   - the coding agent, by **any method its provider supports** (see below);
   - the owner's approval of the engine itself.

#### Coding agent sign-in: every option open

Credentials are their own port, **AgentAuth**, so each provider's methods are adapters
and none is privileged:

| Method | Claude Code | Codex | How the join agent asks |
|---|---|---|---|
| Interactive sign-in (subscription account) | `claude` → browser login | `codex login` | shows the login link / device code on the dashboard and in the terminal |
| Device code (headless machine) | `claude setup-token` run on any signed-in machine | `codex login --device-auth` | shows the code; the engine waits for approval |
| Long-lived OAuth token | `CLAUDE_CODE_OAUTH_TOKEN` | — | paste box |
| API key | `ANTHROPIC_API_KEY` | `OPENAI_API_KEY` | paste box |
| Cloud provider identity | Vertex AI or Amazon Bedrock (`CLAUDE_CODE_USE_VERTEX` / `_BEDROCK` + cloud credentials) | Azure OpenAI | choose project/region; uses the machine's cloud identity, no secret pasted |
| Org gateway / proxy | `ANTHROPIC_BASE_URL` + token | base URL + key | URL + token boxes |

The engine stores what it receives through the Secrets port (mode-600 file by default),
never sends it to mission control, and reports only *which* method is active and
whether a test call succeeds. An engine can hold several (e.g. a subscription login
plus an API key as fallback when a usage limit is hit); the order is configurable.
5. Once approved it receives an **engine token**, installs the factory into the
   assigned product repos (`factory.py init`), and goes `live`.

Engines never get GCP credentials. They talk to one HTTPS **gateway** (Cloud Run)
with their engine token: events go in (the gateway publishes to `factory-events`),
commands come out (a long poll on that engine's `factory-commands` subscription). The
dashboard can revoke an engine token at any time.

### Where agents run

Every event carries `engine`, and node events carry `sandbox` (container id or
worktree). The Console, Board and Flow show each agent's engine, so "app-check on
mac-dima, in container lap-8f3c" is always visible.

## Coordinator (the observer that manages tasks)

Today three things decide what happens next, each on one engine:

| Today | Decides |
|---|---|
| the scheduler (`schedule fire`) | when a lap starts, up to `parallel` per engine |
| `archon-loop`'s **pick** agent | which one move this lap makes; claims the issue with a comment |
| `archon-manager` | on a slower clock: backlog order, unsticking, health, metrics |

With several engines nothing sees the whole fleet. The **coordinator** is a small
service (Cloud Run, subscribed to `factory-events`) that does:

- **Queue:** keeps the ordered queue of workable items per product, from GitHub, the
  backlog's dependencies (`depends on #n` from `archon-backlog`) and claims.
- **Placement:** when an engine has free capacity it sends an `assign` command
  (product, issue, suggested workflow); the engine's lap still claims the issue on
  GitHub, so a lost command or two coordinators can never double-build.
- **Watch:** reassigns work from engines that go silent (the claim is released after a
  heartbeat timeout) and opens problems.
- **Explain:** records *why* each item is next (priority, dependency freed, retry
  after failure), which the Flow screen shows.

It is advisory to the engines' own gates: it chooses *where* and *when*, never whether
something is good enough to merge. Until it exists, engines keep today's behaviour
(each pick agent chooses, claims prevent collisions).

## Flow: done → doing → next

A product-level view of the work in order:

- **Done:** merged/deployed items, newest first, with laps and time taken.
- **Doing:** items in progress, each with its engine, the step it is on, and the agent.
- **Next:** the coordinator's queue in order, with dependency links ("waits for #14"),
  the reason for its position, and which engine it would go to.

Built from `work_items`, `runs` and a new `queue` collection the coordinator writes:
`queue/{product}` → ordered items with `reason`, `depends_on`, `eligible_engines`.

## Git rules: PRs only

Product repos change **only through pull requests**. Nothing pushes to a shared branch:
not a builder, not the holdout writer, not mission control, not a person through the
factory. Other teams work on the same code.

- **Base branch is per-product configuration**, not a convention. Owner's decision
  (2026-11-17): **`main`** for now, kept as a setting that can be changed from the
  console. `products/<product>.yaml` seeds it (`base_branch: main`); the console
  changes it with a `dev.factory.product.updated` command event, so every change has
  who and when, and the `products` projection holds the current value. Nothing in
  mission control assumes a name.
- **Where the pinned engine disagrees.** Archon's shared workflows say "default
  branch": builders branch from it, the manager tests it, and the `holdout-writer`
  brief commits scenarios to it and **pushes** (that is how `d58edec` reached the
  converter's `main` on 2026-11-17). The land gate and app check already use each PR's
  own `baseRefName`. We do not fork the workflows; we extend around them:
  - holdouts are written to a scenario file **outside the repository** (the writer's
    brief: "a scenario file outside the repository is saved, not committed"), kept on
    the engine and backed up to a private bucket through the BlobStore port, later the
    MemoryStore (holdout scope);
  - a read-only **git-rules check** (in `doctor` style) reports per product whether the
    base branch requires a PR and blocks direct pushes, and opens a problem if not;
  - if the owner picks a base branch other than the repo's GitHub default, either the
    default is changed (owner's call) or the gap is raised upstream with Archon; until
    then the factory's builders follow the repo default.
- **The factory needs its own identity** so branch protection, audit and comments
  tell it apart from a person. Today it uses the owner's token. Options:

  | | Owner's token (today) | Machine user | **GitHub App** (recommended) |
  |---|---|---|---|
  | Who GitHub shows | the owner | `factory-bot` (a second account) | `factory-compliance[bot]` |
  | Credential | long-lived PAT | long-lived PAT or SSH key per account | private key → **1-hour** installation tokens |
  | Scope | everything the owner can do | what it is invited to | exact permissions, chosen repos |
  | Per-agent least privilege | no | one account per role (seats, ToS limits) | one App per role, free |
  | Advisory signal | comment | comment | **neutral Check Run** (only Apps can create them) and comment |
  | Branch protection | owner may be an admin who bypasses | normal collaborator | listed or excluded explicitly |

  Recommendation: **two GitHub Apps** owned by the owner's account: `factory-builder`
  (contents + pull requests write, on product repos; never admin) and
  `factory-compliance` (pull requests + contents **read**, checks + issues **write** for
  a neutral check and one comment). Each App's private key lives only on the main node
  (Secret Manager, read by `factory-vm`); the main node mints 1-hour, repo-scoped
  installation tokens and hands them to an agent per job. No GitHub secret is ever
  stored in a pod. Until the Apps exist, the compliance agent reads the public
  converter repo **unauthenticated** and writes nothing to GitHub.

## Projects and agents status

One console for every product and every agent.

- **Products registry.** `products/<product>.yaml` in the factory repo (repo, base
  branch, tracker, assigned pools, compliance mode) is published as
  `dev.factory.product.registered`; the `products` collection is its projection plus
  rollups: open PRs, laps today, last lap result, open problems, compliance findings,
  and `git_seen_at` (how fresh the git data is).
- **Git changes keep it current** through the **GitActivity** port. The first adapter
  polls GitHub (every 60 s per product, ETag-conditional, so it costs nearly nothing and
  needs no public endpoint, which the sandbox may not allow). The webhook adapter
  (`gh-ingest`, Stage G) is a later swap with no core change. Both publish the same
  `dev.factory.workitem.*`, `dev.factory.pr.*`, `dev.factory.check.*` events.
- **Agents.** Every running instance, whatever it is (an engine VM, a GKE pod, a
  laptop), heartbeats as `dev.factory.agent.heartbeat` with: `agent_id`, `pool`, `kind`
  (`engine`, `compliance`, …), `location` (`gce:me-west1-a/factory-1`,
  `gke:factory-agents/compliance-7f9c`, `host:mac-…`), state (`starting idle busy
  draining silent`), current run and node, model and tier, auth method (never the
  secret), version (image digest or Archon pin). The `agents` collection is the
  projection; `engines` stays for the bridge cursor.

## Agent fleet and automatic scale-out

"Add more agents automatically" means a **desired state** that a reconciler makes true.

```yaml
# fleet.yaml (factory repo); the dashboard edits the same values through a command
pools:
  - name: compliance
    kind: compliance
    runtime: gke              # AgentRuntime adapter
    image: me-west1-docker.pkg.dev/<project>/mission-control/agent:<digest>
    products: [file-to-markdown-convertor]
    replicas: {min: 1, max: 3}
    scale_on: pending_prs     # or: queue_depth, schedule, fixed
    model: {tier: small, effort: low}
    mode: advisory
  - name: builders
    kind: engine
    runtime: gce
    replicas: {min: 1, max: 1}
```

- **AgentRuntime port** (deploy, scale, list, drain, delete) with adapters for GKE,
  GCE VMs (wrapping `create-factory-vm.sh`) and local Docker; one contract suite.
- **Reconciler**: compares `fleet.yaml` (plus dashboard overrides) with what
  `AgentRuntime.list` reports and with demand (pending PRs, coordinator queue depth),
  then scales within `min..max`. It runs as a Cloud Run job every minute; on GKE the
  same rule can be delegated to a Kubernetes autoscaler (KEDA on a Pub/Sub backlog) as
  an adapter detail.
- **From the console**: "Add agent" raises a pool's replicas through a
  `dev.factory.command.scale` command; "Drain" lets the current work finish first.
- **Rebuildable**: the sweep deletes GKE clusters and VMs. The fleet's desired state
  lives in git and Firestore, images in Artifact Registry (not swept), so
  `rebuild.sh` recreates the cluster and the reconciler brings every pool back.

## GKE agents and the compliance agent

> **Changed 2026-11-21.** No Vertex AI for now, and GKE is parked. The compliance agent
> and later agents run as Docker containers on factory-1 or on sandbox VMs, use Claude
> the way the project does, and are installed, deployed and chained from the console:
> [deploy-and-hosts-plan.md](deploy-and-hosts-plan.md). The agent design below (rules,
> reviewer, reports, empty rules cost nothing) still holds; the GKE and Vertex parts are
> kept for a later option.

The first agent on GKE is a **compliance agent**: it reviews every open PR of the
products it is assigned to against written compliance rules and reports findings.

**Assumption, from the owner's "put it to work on zero effort for now":** it runs
continuously but **advisory only**. It reports, and never blocks a merge (no required
check, no failing status, no review "request changes"), at the **lowest reasoning
effort and the cheapest model tier**, with a daily spend cap. Changing to enforcing
mode is a later, explicit owner decision (it would then post a required check that
`required_checks` lists, so the land gate refuses until it is green).

**Empty rules cost nothing.** The rule set starts empty (owner's decision). With no
rules the agent still polls, records each PR head as reviewed against rules version
`empty@<hash>`, and publishes a report with `status: no_rules`, **without calling a
model**. The first rule added in the prompt manager turns on model reviews for the next
head; earlier reports keep the version they used.

- **Agent**: a small Python process (`agents/compliance/`), not an Archon workflow. One
  review per PR head is a single model call; Archon would add a checkout and a bridge to
  every pod for nothing. The agent publishes its own CloudEvents through the EventBus
  port: `dev.factory.compliance.reported` (findings, severity, rule ids, reviewed head
  SHA, rules version, cost) and `dev.factory.agent.heartbeat`. Loop: list open PRs of
  each assigned product → skip heads already reported → fetch the diff → load rules
  from the **PromptRegistry** (label `production`) → review (`claude -p`, Haiku tier,
  low effort, read-only tools) → publish.
- **Where reports go**: Firestore `compliance/{product}__{pr}` (projection) and the
  console's Projects view. A neutral Check Run and one comment edited in place come with
  the `factory-compliance` GitHub App (see Git rules).
- **Sandbox**: the pod is the sandbox. It reads diffs over the GitHub API; there is no
  checkout, no write token and no shell tool for the model.

### Security: everything through the main node

Owner's rule (2026-11-17): the cluster and its agents are reachable and controllable
**only from the main node (factory-1)**, the console is used **only through the main
node**, and no Claude credential sits in a pod.

```
 operator laptop ──IAP (SSH -L, Google login + IAM)──► factory-1 (main node)
                                                         │  console on 127.0.0.1
                                                         │  kubectl (only identity allowed)
                                                         ▼
                              GKE Autopilot "factory-agents" (private nodes,
                              control plane: DNS endpoint gated by IAM + internal IP
                              with authorized network = factory-1 only)
                                │ Workload Identity (no keys)
                 ┌──────────────┼──────────────────────┐
                 ▼              ▼                      ▼
     Vertex AI (Claude)   Pub/Sub factory-events   GitHub API (Cloud NAT,
     roles/aiplatform.user  publisher only          later FQDN policy: github only)
```

1. **Claude on Vertex AI with Workload Identity.** The pod's Kubernetes service
   account is bound to `roles/aiplatform.user`; Claude Code runs with
   `CLAUDE_CODE_USE_VERTEX=1` and gets short-lived Google tokens from the metadata
   server. **No Claude secret exists anywhere.** This is also the clean fit for
   Anthropic's terms: a subscription token (`claude setup-token`) is meant for one
   person's ordinary use, while unattended fleets belong on commercial access (Vertex,
   an API key, or Anthropic's own workload-identity federation). The subscription
   token stays for the owner's interactive work on factory-1 only.
   - Checked live 2026-11-17: Claude models (Haiku 5.5, Sonnet 5.5, Opus 5.5…) are
     listed in Model Garden, but calls return "project does not have access": the
     owner must **enable the Anthropic models once in the Model Garden console**
     (accepting the publisher's terms). The `vertexai.allowedModels` policy allows all.
   - Region: try `me-west1` first; else `CLOUD_ML_REGION=global` (data location not
     guaranteed) or `eu`. Recorded per pool.
2. **Private cluster.** Autopilot, private nodes, control plane with the **DNS-based
   endpoint** (IAM-gated) and the IP endpoint private, authorized network = factory-1's
   internal address. Only `factory-vm` (the main node's service account) is granted
   `container.clusters.connect` (Kubernetes Engine Developer). The Mac never runs
   kubectl against it; it creates the cluster through the Google API and everything
   inside runs from factory-1.
3. **Egress.** Cloud NAT for pods (GitHub). Google APIs go over Private Google Access.
   Later: an FQDN network policy allowing only `github.com`/`api.github.com` (Autopilot
   enables it by cluster update).
4. **Console only through the main node.** Dashboards and Archon's UI bind to
   `127.0.0.1` on factory-1; the operator opens them with
   `gcloud compute ssh factory-1 --tunnel-through-iap -- -L 8080:localhost:<port>`
   (IAP needs the 35.235.240.0/20 → tcp:22 rule and `iap.tunnelResourceAccessor`).
   factory-1 then needs **no public IP** (egress through the same Cloud NAT); removing
   it is a separate step with the owner's OK.
5. **GitHub.** App private keys only on the main node; per-job 1-hour tokens to pods
   (see Git rules).
6. **Org policy, checked live 2026-11-17** in the sandbox: external IPs, service
   account keys, locations, load balancers, Cloud NAT, Vertex models are all allowed;
   OS Login is enforced; Workload Identity and private nodes are allowed, not forced.
   The sandbox sweep deletes GKE clusters, firewall rules and VMs every weekend, so the
   cluster, NAT and rules are created by `bin/gke/create-cluster.sh` from `rebuild.sh`.

## Workflow browser and agent messages

A **Workflows** view: every workflow definition (pinned pack and project-level), its
runs, each run's graph, and for each node the **conversation** between agents:
orchestrator → subagent dispatch prompt → the subagent's tool calls → its reply → the
node's final output.

- **Source**: Archon's `provider_event` rows (verified above). New event types:
  `dev.factory.agent.dispatched` (from node/agent, to `subagent_type`, prompt),
  `dev.factory.agent.replied` (subtask summary, usage), `dev.factory.agent.message`
  (assistant text), `dev.factory.tool.called` / `dev.factory.tool.returned`.
- **Size**: bodies over the 16 KB event cap go to the **BlobStore** (content hash as
  the key); the event and the `runs/{run}/messages/{seq}` document carry a 2 KB preview
  plus the blob reference. The tool-activity rollup per node (H4) stays for the live
  console; the message log is the full record.
- **Redaction**: holdout agents are metadata-only (see the envelope rules); a secret
  scanner runs on every body; bodies keep the run's retention (90 days).
- **Workflow catalog**: definitions come from each run's captured source
  (`metadata.workflow_source`) and from `consumer.py list`, published as
  `dev.factory.workflow.declared` and kept in `workflows/{name}@{digest}`.

## Memories browser

A **Memories** view per product over the **MemoryStore** port (Stage I): mission,
decisions, conventions (`engineering.md`), journeys, lessons and agent-written notes,
each with versions, a diff between versions, and where a change came from (PR or run).

- Read-only by default. "Propose a change" opens a **pull request** through the
  CodeHost port (git-files adapter) or a pending version that needs approval (database
  adapter). Never a direct write to a shared branch.
- **Holdouts are never shown as text** in mission control: the browser lists scenario
  ids, the product rule each protects, and verdict history only. The text stays with
  the owner's editor and the holdout agents.
- The `memory_index` collection holds metadata (title, kind, version, size, updated,
  source link), never the text of holdouts.

## Prompt and skill managers

Designed as **independent services** with their own APIs, usable without mission
control: [prompt-skill-manager-design.md](prompt-skill-manager-design.md). Mission
control is a client: the console shows which prompt and skill versions each run pinned
(`dev.prompts.lock.pinned`), and links to the managers for browsing and diffs.

## Security and cost

- **Identities:** VM service account `factory-vm` (Pub/Sub publisher on
  `factory-events`, Firestore user for its `engines` document); writer service account
  `factory-writer` (Pub/Sub subscriber, Firestore user); gh-ingest validates the
  GitHub webhook secret from Secret Manager.
- **Data:** events can carry code and agent output. The bus and store stay inside the
  project; redaction runs in the bridge; holdout text is never published.
- **Cost:** a lap produces on the order of a few hundred events after the tool-activity
  rollup: Pub/Sub and Firestore costs stay in single dollars per month at this scale.

## Phases

| Phase | Deliverable | Done when |
|---|---|---|
| **P0** | Archon console on the VM through an IAP tunnel; Pub/Sub topic + DLQ; Firestore; service accounts; **archon-bridge** publishing Archon runs/events and heartbeats; **writer** projecting `events`, `runs`, `nodes`, `engines` | a run on the VM appears in Firestore within seconds, and is still there after the VM is rebuilt |
| **P1** | Dashboard: **History**, **Problems**, **Engines**; problem rules; factory-file events | a failed or rerun lap shows with its retry chain and an open problem |
| **P2** | **Console** (live graph, agents, handoffs, tool activity), **Board**, gh-ingest, Slack alerts | a running lap is watchable end to end, parallel branches included |
| **P3** | **Gateway** + engine tokens; **join agent** (toolchain, Docker sandbox, auth prompts, approval); **Engines** screen; `factory-commands` (halt/approve/rerun) | a laptop joins with one command, asks for its logins, and its laps show on the dashboard |
| **P4** | **Coordinator** (queue, placement, reassignment, reasons) and the **Flow** screen; second product | work spreads over two engines, and Flow shows done → doing → next with the reason for each |
| **A1** (early, owner's near-term goal) | PR-only guardrails; agent image; GKE cluster script; **compliance agent** on GKE in advisory mode | every new PR commit on the converter gets a compliance report in Firestore within 10 minutes, and no merge is ever blocked by it |
| **A2** | Projects and Agents views; GitHub polling; fleet reconciler and scale-out | a push shows in Projects within 2 minutes; raising a pool's max adds pods that appear in Agents |
| **A3** | Workflows view with agent messages; Memories view; prompt and skill managers | a lap's dispatch prompts and replies are readable per node (holdout agents metadata-only); a run shows its pinned prompt versions |

## Open questions

- Does the sandbox sweep also remove Cloud Run services, Firestore or Pub/Sub? Pub/Sub
  topics in the project have survived past sweeps; the rest is checked in P0.
- The upstream factory's `DECISIONS.md` mentions a "mission-control dashboard". Ask
  upstream whether one exists before P1 to avoid building it twice.
- ~~Base branch~~: decided 2026-11-17, `main`, changeable per product from the console.
- **Factory GitHub identity**: options laid out under Git rules (two GitHub Apps
  recommended); owner's decision, open.
- ~~Compliance rules~~: decided 2026-11-17, start **empty**; the owner adds and changes
  rules in the prompt manager, with versions.
- **Agent messages, unverified details** (only one AI run inspected so far):
  whether a subagent's own tool calls are attributed to it (`tool_call` rows carry no
  `parentToolCallId`; `subtask` rows carry `lastToolName` only); whether
  `agent_message_chunk` holds every assistant turn or only the last (the inspected node
  had one chunk, equal in size to `node_output`); the truncation threshold behind
  `outputTruncated`; whether the rendered prompt of a node's own agent (not a
  subagent's) is stored anywhere.
- **Project-level workflows**: does the pinned Archon load workflows from a directory
  outside the product repo (Archon home or a configured path)? Needed for the compliance
  workflow without committing it to the product repo.
- ~~Subscription token in pods~~: decided, no. Pods use Claude on Vertex AI through
  Workload Identity (see Security). Waiting on the owner to enable the Anthropic models
  in Model Garden.
- **GKE in the sandbox**: org policy allows it (checked); Autopilot quota in me-west1
  is checked when the cluster is first created.
- **Public endpoints**: whether org policy allows an unauthenticated Cloud Run service
  (needed for GitHub webhooks); polling avoids the question.

## Code layout

```
mission-control/
  schema/            CloudEvents types + JSON Schemas, AsyncAPI, OpenAPI
  core/              ports (interfaces), projections, problem rules: imports ports only
  adapters/
    bus/             pubsub/, memory/ (in-process fake), nats/ (later)
    store/           firestore/, memory/, postgres/ (later)
    tracker/         github/, jira/ (later), mirror/ (tracker → GitHub for the engine)
    codehost/        github/
    memory/          gitfiles/, firestore/ (later), materializer/
    engine/          archon/
    notifier/        slack/
  contract_tests/    one suite per port, run against every adapter
  bridge/            engine-side process: EngineAdapter → EventBus
  writer/            bus → projections → StateStore (Cloud Run)
  gateway/           engine tokens, events in, commands out (P3)
  coordinator/       queue and placement (P4)
  fleet/             reconciler: fleet.yaml + demand → AgentRuntime
  adapters/runtime/  gke/, gce/, docker/
  adapters/blob/     gcs/, memory/
  adapters/git/      github_poll/, github_webhook/
agents/
  compliance/        project-level Archon workflow, rules.md, scripts
  image/             Dockerfile for the agent image (Archon pin + Claude Code + gh + bridge)
products/            one YAML per product: repo, base_branch, pools, compliance mode
fleet.yaml           desired agent pools
bin/gke/             create-cluster.sh, deploy.sh (called from rebuild.sh)
  web/               dashboard
  infra/             gcloud scripts
  mission-control.yaml   which adapter each port uses
```

The step-by-step build order is in [mission-control-plan.md](mission-control-plan.md).
