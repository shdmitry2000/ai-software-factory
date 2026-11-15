# Mission control: design

A live console and history for the factory: what is running, what finished, what
failed or was rerun, where each piece of work is, and how the agents handed work to
each other. Built on GCP Pub/Sub and Firestore, outside the factory VM, so history
survives the weekly sandbox sweep.

Status: design agreed 2026-11-15; P0 in progress. Pinned Archon: `1cfb8fc5`.

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
  the bus too.

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

A single-page web app reading Firestore directly through live listeners (no
WebSocket or SSE server of our own). Hosted on Cloud Run behind Google sign-in limited
to the org domain; Firestore security rules make it read-only for those users, and
only the writer's service account can write.

| View | Answers | Reads |
|---|---|---|
| **Console** | What is running right now, on which step, by which agent, talking to whom | `runs` where status in (running, paused), their `nodes`, `edges`, live tool rollup |
| **Board** | Where each work item is, and what blocks it | `work_items` by stage |
| **History** | What finished / failed / was rerun, how long, how much | `runs` by time, grouped by work item; filters: product, workflow, status, engine |
| **Problems** | What needs a human now | `problems` where open, by severity |
| **Engines** | Is each factory alive, halted, paused, on which pin | `engines` |

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

## Open questions

- Does the sandbox sweep also remove Cloud Run services, Firestore or Pub/Sub? Pub/Sub
  topics in the project have survived past sweeps; the rest is checked in P0.
- The upstream factory's `DECISIONS.md` mentions a "mission-control dashboard". Ask
  upstream whether one exists before P1 to avoid building it twice.

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
  web/               dashboard
  infra/             gcloud scripts
  mission-control.yaml   which adapter each port uses
```

The step-by-step build order is in [mission-control-plan.md](mission-control-plan.md).
