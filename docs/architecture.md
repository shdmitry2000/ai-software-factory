# AI Software Factory: architecture

The whole system on one page set: what runs where, how data and control move, who may
do what, and what is built versus planned. Detailed designs and build steps live in
the documents linked from each section.

Status: 2026-11-21 (hosting changed: Docker on servers, no Vertex, GKE parked). Legend used throughout: **running** (deployed and working),
**built** (code and tests exist, not deployed), **planned** (designed, not built).

| Document | What it holds |
|---|---|
| this file | the architecture: components, flows, security, data, operations |
| [mission-control-design.md](mission-control-design.md) | mission control in depth: events, projections, problems, fleet, GKE agents |
| [console-ui-plan.md](console-ui-plan.md) | the operator console: access, screens, actions, build steps |
| [deploy-and-hosts-plan.md](deploy-and-hosts-plan.md) | hosts, deploy, process control and agent chains, all from the console |
| [prompt-skill-manager-design.md](prompt-skill-manager-design.md) | prompt and skill managers (independent services) |
| [mission-control-plan.md](mission-control-plan.md) | the build plan in small, checkable steps |
| [gcp-runbook.md](gcp-runbook.md) | how to create, sign in to and rebuild the GCP setup |
| [../README.md](../README.md) | the factory itself: what it does, commands, the files that are yours |

---

## 1. What the system does

The factory turns a release plan (PRD) into merged, reviewed pull requests on a
product repository. Archon workflows drive coding agents through issue → build →
independent review → merge gate → deploy to staging. The owner decides what to build
and judges staging; everything between runs on its own.

Around that core sit:

- **Mission control**: every run, agent and product made visible and controllable
  from one console, with history that survives the weekly sandbox sweep.
- **Agents outside the engine**: long-running agents in Docker containers on the main
  node or on sandbox VMs, installed, deployed and chained from the console, starting
  with an advisory **compliance agent** that reviews every open PR against versioned
  rules.
- **Prompt and skill managers**: versioned, labelled, reviewable prompts and skills.

## 2. Principles (the rules everything below follows)

1. **Pull requests only.** Product repos change only through PRs. Nothing, the
   factory included, pushes to a shared branch. The PR base branch is a per-product
   setting (`main` today), changeable from the console.
2. **Everything through the main node.** Agent hosts, the console and every control
   action are reachable only from the main node (factory-1); only it deploys. No
   public endpoints. Operators reach the main node through Google IAP.
3. **Secrets stay narrow.** Google access uses service-account identity, never keys.
   The LLM credential (the project's Claude login, or an API key per agent) lives in
   the repo's `.factory-secrets/` on the owner's Mac, and the main node pushes it over
   SSH stdin into a root-only env file on the host that runs the agent. Never in an
   image, git, an event or a log.
4. **Ports and adapters.** Core code talks to interfaces (`mission_control/ports.py`);
   which product serves each port (Pub/Sub, Firestore, GitHub, MLflow…) is
   configuration. A new adapter must pass the port's contract tests.
5. **Events are the record.** Every source publishes CloudEvents 1.0 to one bus; all
   readable state (projections) is rebuilt from events, order-independent and
   idempotent.
6. **Rebuildable.** The sandbox deletes VMs, clusters, firewall rules and subnets every
   weekend. Durable state lives only in Pub/Sub, Firestore, Artifact Registry, Cloud
   Storage, GitHub and git. Everything else is recreated by scripts.
7. **Holdouts stay hidden.** Hidden acceptance scenarios are readable only by the
   holdout agents; mission control sees their ids and verdicts, never their text.
8. **Advisory before enforcing.** New judges (compliance) report first; turning one
   into a merge blocker is an explicit owner decision.
9. **Stay in the folder.** On the owner's Mac all tool state (gcloud, docker, kubectl,
   gh, Claude) lives under the repo's `.factory-secrets/`, never in `$HOME`.

## 3. System context

```
                 ┌──────────────┐
   owner ───────▶│ Mac (repo +  │── IAP (Google login) ──┐
   (browser)     │ .factory-    │                        │
                 │  secrets/)   │                        ▼
                 └──────────────┘          ┌────────────────────────────┐
                                           │ MAIN NODE  factory-1 (GCE) │
   GitHub ◀── PRs, reads ──────────────────│ engine · bridge · console  │
   (product repos,                         │ kubectl (only it)          │
    factory repo)                          └──────┬──────────┬──────────┘
        ▲                                         │          │
        │ reads diffs (NAT)                       │ events   │ deploys
        │                                         ▼          ▼
   ┌────┴─────────────────┐   events    ┌──────────────┐  ┌─────────────────────┐
   │ agent hosts (Docker) │────────────▶│ Pub/Sub +    │  │ Artifact Registry   │
   │ factory-1 + sandbox  │             │ writer +     │  │ (images)            │
   │ VMs, no public IP    │             │ Firestore    │  └─────────────────────┘
   └──────────┬───────────┘             └──────────────┘
              └── Claude (project login / API key) ──▶ Anthropic
```

External systems: **GitHub** (code host, tracker today), **Anthropic** (Claude for
the engine and the agents, as the project uses it), **Google IAP** (operator access).
Vertex AI is a possible later option per agent.

## 4. Components

| Component | Where | What it does | State |
|---|---|---|---|
| **Engine: Archon** (pinned `1cfb8fc5`) | factory-1 | runs the SDLC workflows: triage, build, review, holdout verify, merge gate, deploy; agents in Docker sandboxes; SQLite DB of runs and events | running |
| **Factory consumer** (`factory/consumer.py`) | factory-1 | doctor, list, run, halt; the schedule that starts laps | running |
| **Archon console** (`archon serve`) | factory-1 :3090 | Archon's own deep view of one run | running (to move behind the main-node console, see §9) |
| **archon-bridge** | factory-1 | reads Archon's DB, maps lifecycle events to CloudEvents, publishes them and a heartbeat every 60 s; carries no agent output text | running |
| **Event bus** | Pub/Sub `factory-events` (+ `factory-events-dlq`, `factory-commands`) | one ordered stream of everything that happens | running |
| **Writer** (`mission-control-writer`) | Cloud Run, push subscription `factory-events-writer` | validates each event, stores it, updates projections | running |
| **State store** | Firestore `(default)` | `events`, `runs`, `nodes`, `engines`; next `agents`, `compliance`, `products`, `problems`, `work_items` … | running |
| **Console** | factory-1, `127.0.0.1` only | operator UI: status, history, live runs, settings, commands | planned ([console-ui-plan.md](console-ui-plan.md)) |
| **Command handler** | factory-1 | executes console commands (halt, settings, scale) from `factory-commands` | planned |
| **Agent hosts** | factory-1 (`local`) and sandbox VMs (`vm`) running Docker | run agents and, later, other system parts | factory-1 running; VM hosts planned (Stage R) |
| **AgentRuntime** (in the command handler) | factory-1 | install hosts; deploy, start, stop, upgrade, roll back, scale, logs; restore after the sweep | port + `fake` + `docker-local` built (`python3 -m mission_control.runtime`); handler planned ([deploy-and-hosts-plan.md](deploy-and-hosts-plan.md)) |
| **Compliance agent** | container (`agents/compliance`, image `mission-control/agent`) | advisory review of every open PR head against versioned rules | built; first deploy on factory-1 (R2) |
| GKE cluster `factory-agents` | – | parked 2026-11-21; scripts kept in `bin/gke/`, not rebuilt after the sweep | parked |
| **Prompt manager** | git `prompts/` now; MLflow later | versioned, labelled prompts (compliance rules first) | built (file adapter) / planned (service) |
| **Skill manager** | git plugin marketplace | versioned skills and subagents materialized per lap | planned (Stage T) |
| **GitHub poller** | main node | PR, push and check events for every product (no public webhook needed) | planned (Stage Q) |

## 5. Deployment view (GCP project `me-sb-dgcp-dpoc-pocyosh-pr`, region me-west1)

```
 VPC "default"  subnet default/me-west1 (Private Google Access on)
 ┌──────────────────────────────────────────────────────────────────────────────┐
 │  factory-1  10.208.0.38  e2-standard-2  Ubuntu 24.04  SA factory-vm          │
 │    Archon + consumer + bridge + archon-console(:3090) [+ console, planned]   │
 │                                                                              │
 │    Docker: compliance agent container (planned R2)                           │
 │                                                                              │
 │  vm-agents-N  (planned)  no public IP, SA factory-agent, Docker; SSH only    │
 │    from 10.208.0.38; created, installed and deployed from the console        │
 │                                                                              │
 │  Cloud Router factory-router + Cloud NAT factory-nat  (egress: GitHub)       │
 │  Firewall: 35.235.240.0/20 → tcp:22 (IAP SSH)  [tcp:3090 rule to retire]     │
 └──────────────────────────────────────────────────────────────────────────────┘
  Serverless / managed (survive the sweep):
    Pub/Sub  factory-events, factory-events-dlq, factory-commands
    Cloud Run  mission-control-writer
    Firestore (default), native mode
    Artifact Registry  me-west1/mission-control  (writer and agent images)
```

Other workloads in the project (`rag-eval-bank` cluster and its rules) are not part
of the factory and are never touched by its scripts.

## 6. Identities and permissions

| Identity | Used by | Grants (least privilege) |
|---|---|---|
| owner (Google account) | Mac scripts, IAP | project owner; IAP tunnel |
| `factory-vm` SA | factory-1 | Pub/Sub publish `factory-events`; Firestore user; Artifact Registry writer on `mission-control`; creates agent VMs (planned) |
| `factory-writer` SA | Cloud Run writer | Pub/Sub subscriber; Firestore user; publish `factory-commands` |
| `factory-agent` SA (planned) | agent VM hosts | Pub/Sub publish `factory-events` and subscribe to its own subscriptions; Firestore viewer; Artifact Registry reader |
| `factory-vm` on agent hosts (planned) | factory-1 deploying | `compute.osAdminLogin` on agent hosts only (SSH for deploys) |
| LLM credential | engine and agents (`llm: project` or `api_key`) | root-only env file on the host that runs it; pushed by factory-1 over SSH stdin |
| GitHub: owner's token today | factory-1 engine | to be replaced by GitHub Apps |
| GitHub Apps (planned) `factory-builder`, `factory-compliance` | engine, compliance agent | builder: contents + PR write; compliance: read + checks/issues write. Keys only on the main node; 1-hour tokens per job |

The GKE identities (`factory-gke-node`, KSA `agents/compliance`) are parked with the cluster.

## 7. Security model

```
 operator ─▶ IAP (Google login, iap.tunnelResourceAccessor) ─▶ SSH to factory-1
              └─ port forward -L ─▶ console on 127.0.0.1 (factory-1)
 factory-1 ─▶ agent VM hosts (SSH, internal IP, firewall source 10.208.0.38 only) ─▶ docker
 containers ─▶ Google APIs (Private Google Access, host service account)
 containers ─▶ GitHub, Anthropic (Cloud NAT)
 nobody ─▶ containers (no published ports, no public IPs)
```

- **No public entry points.** No load balancer, no public Cloud Run service for
  operators, no webhook endpoint (GitHub is polled). factory-1's public IP is to be
  removed once NAT carries its egress (plan OB7, owner's OK).
- **Secrets inventory.** Owner's Mac: `.factory-secrets/` (gcloud, docker, kube, gh,
  Claude token; mode 700, git-ignored, denied to agents). factory-1: `~/.factory-env`
  (owner's Claude and GitHub logins, delivered over stdin). Agent hosts:
  `/etc/factory/<component>.env` (root, 600), the LLM credential only. Later: GitHub
  App keys in Secret Manager, readable by `factory-vm` only.
- **Data exposure.** Events carry metadata, never holdout text; agent message capture
  (Stage S) redacts holdout agents to metadata and scans bodies for secrets.
- **Agent sandboxing.** Engine agents run in Docker sandboxes on factory-1. The
  compliance container runs non-root, read-only filesystem, all capabilities dropped,
  no tools for the model, no write token.
- **Audit.** Every console action becomes an event (who, what, when) in the same log.

## 8. Data flows

### 8.1 A lap (engine work on a product)

```
 schedule tick ─▶ consumer.py run ─▶ Archon workflow
   pick issue ─▶ build (agent, sandbox, branch) ─▶ validate ─▶ app check
   ─▶ holdout verify ─▶ review (2 agents that did not write it) ─▶ merge gate
   ─▶ PR to base_branch (from products/<product>.yaml) ─▶ deploy to staging
 every step ─▶ Archon DB ─▶ bridge ─▶ factory-events ─▶ writer ─▶ runs/, nodes/
```

### 8.2 The event pipeline

```
 source (bridge | agent | poller | console)
   └─▶ CloudEvent {id deterministic, type dev.factory.*, subject, time, data}
        └─▶ Pub/Sub factory-events (ordering key per run / agent / PR)
             └─▶ push ─▶ writer: validate (schema/) ─▶ events/{id} (dedupe)
                                   ─▶ projections (runs, nodes, engines, agents,
                                       compliance, products, problems …)
             └─▶ after 5 failures ─▶ factory-events-dlq (held for inspection)
 console ◀── Firestore listeners (server side, on factory-1)
```

### 8.3 Compliance review (advisory)

```
 every 10 min (later also on PR events), compliance container:
   rules  = PromptRegistry.get("compliance/rules", label="production")
   for each open PR (GitHub, read-only):
     skip if this head was already reported against this rules hash
     rules empty  ─▶ report status no_rules        (no diff, no model call, $0)
     spend ≥ cap  ─▶ report status capped           (no model call)
     else diff ─▶ claude -p (Haiku, low effort, no tools, project LLM login) ─▶ findings
   publish dev.factory.compliance.reported {head, base, rules version+hash, findings, cost}
 writer ─▶ compliance/{product}__{pr} (latest report wins)
 later (GitHub App): neutral Check Run + one comment edited in place; never blocks
```

### 8.4 Console action (command)

```
 operator clicks (e.g. "base branch → main", "halt", "add agent")
   ─▶ console server (factory-1) ─▶ dev.factory.command.issued on factory-commands
   ─▶ command handler executes (edit setting / halt engine / scale pool)
   ─▶ dev.factory.command.acknowledged + the resulting event (product.updated …)
   ─▶ writer projects; console updates live; audit trail = the events
```

### 8.5 Rule or prompt change

```
 owner edits rules in the console (or a PR to prompts/)
   ─▶ new immutable version (0002.md) ─▶ label production moves to 2 (PR, reviewed)
   ─▶ next compliance tick loads version 2 ─▶ open PRs reviewed again
   ─▶ old reports keep version 1 (reproducible); rollback = move the label back
```

### 8.6 Deploy from the console

```
 operator: Deploy compliance → vm-agents-1   (or add a VM host, start, stop, upgrade …)
   ─▶ console ─▶ command.issued ─▶ handler on factory-1 ─▶ AgentRuntime
        local: docker on factory-1      vm: docker over SSH (internal IP)
   ─▶ pull by digest → env file over stdin → Pub/Sub subscriptions (chain) → run
      → first heartbeat;  each step an event dev.factory.deploy.step
   ─▶ deployment.applied ─▶ projections hosts/, deployments/, agents/ ─▶ console live
```

Details: [deploy-and-hosts-plan.md](deploy-and-hosts-plan.md).

### 8.7 Weekly sweep and rebuild

```
 Fri 21:00 UTC  halt the factory (consumer.py halt)
 Sat–Sun        sandbox cleaner deletes VMs, disks, firewall rules, subnets, GKE,
                disables APIs. Pub/Sub, Firestore, Cloud Run, images survive.
 Sun/Mon        bin/gcp/rebuild.sh: enable APIs → factory-1 → logins (bin/login.sh,
                push-logins.sh) → app setup → bridge → console
                → "Restore all" in the console: VM hosts recreated, agents
                redeployed at pinned digests; reviewed heads are read back from
                Firestore, so nothing is reported twice
```

## 9. Console (operator UI)

Runs on factory-1, bound to `127.0.0.1`, opened through an IAP SSH tunnel
(`bin/gcp/console.sh`). One server reads Firestore with the main node's identity and
streams changes to the browser; every action is a command event. Screens: Overview,
Projects (with product settings such as base branch), Agents, Hosts, Deploy, Chains,
Runs (history and live graph), Workflows (agent messages), Compliance, Problems,
Memories, Prompts & Skills, Audit. Archon's own console moves behind the same tunnel
and is linked per run. Full plan: [console-ui-plan.md](console-ui-plan.md).

## 10. Data model (Firestore collections)

| Collection | One document per | Written by | State |
|---|---|---|---|
| `events/{id}` | event (raw, deduped) | writer | running |
| `runs/{run}` | workflow run: status, times, graph, failure | writer | running |
| `nodes/{run}:{node}` | step of a run | writer | running |
| `engines/{engine}` | engine heartbeat and bridge cursor | writer / bridge | running |
| `agents/{agent_id}` | agent instance: host, state, model, LLM method, spend | writer | built |
| `hosts/{host}` | host: kind, state, health, what runs there | writer | planned (R) |
| `deployments/{id}` | component, host, digest, replicas, state, last steps | writer | planned (R) |
| `compliance/{product}__{pr}` | latest advisory report of a PR | writer | built |
| `products/{product}` | settings (seeded from `products/*.yaml`, changed by console) + rollups | writer | planned (Q) |
| `problems/{id}` | open/resolved problems | rules | planned (F) |
| `work_items/{id}` | issue → laps → PR → merge path | writer | planned (G) |
| `runs/{run}/messages/{seq}` | agent-to-agent messages (holdouts: metadata) | writer | planned (S) |
| `workflows/{name}@{digest}` | workflow definitions | writer | planned (S) |
| `memory_index/{product}/{doc}` | knowledge documents metadata | writer | planned (U) |
| `commands/{id}` | console commands and their outcome | writer | planned (M) |

Event types are listed in [`mission-control/schema/types.md`](../mission-control/schema/types.md)
(closed list; data schemas in `data.v1.json`).

## 11. Ports and adapters

| Port | Purpose | Adapters (state) |
|---|---|---|
| EventBus | publish/pull events | memory (built), pubsub (running) |
| StateStore | projections | memory (built), firestore (running) |
| EngineAdapter | read engine events | archon (running) |
| CodeHost | PRs, heads, diffs | github REST read-only (built) |
| PromptRegistry | versioned prompts + labels | files (built), mlflow (planned) |
| WorkTracker | issues, claims | github (planned), jira (planned) |
| MemoryStore | product knowledge, holdout scope | gitfiles, firestore (planned) |
| Secrets | named secrets | Secret Manager (planned) |
| Notifier | alerts | slack (planned) |
| AgentRuntime | install hosts; deploy, control, logs, restore | fake, docker-local (built); docker-ssh, gce-vm (planned); gke (parked) |
| BlobStore | large bodies, holdout backups | gcs, memory (planned) |
| GitActivity | repo changes as events | github poll (planned), webhook (later) |
| SkillRegistry | versioned skills | git plugin marketplace (planned) |

## 12. Code layout

```
bin/                 login.sh, factory.py, gcp/ (VM, logins, rebuild, env.sh), agents/ (deploy on factory-1), gke/ (parked)
agents/              compliance/ (agent, rules, reviewer, tests), image/Dockerfile
deploy/              (planned) components/*.yaml, hosts.yaml, deployments.yaml
products/            one YAML per product: repo, base_branch, compliance
prompts/             PromptRegistry file adapter: <name>/NNNN.md + labels.yaml
mission-control/
  mission_control/   ports, registry, config, events, projections, bridge, writer, products
    adapters/        bus/, store/, engine/archon, codehost/github, prompts/files, runtime/ (fake, docker_local)
  schema/            envelope, data schemas, types.md, examples
  contract_tests/    one suite per port
  tests/             unit tests
  infra/             setup.sh, deploy-writer.sh
template/            files installed into product repos
docs/                this documentation
```

## 13. Decisions log

| Date | Decision |
|---|---|
| 2026-11-15 | Mission control on Pub/Sub + Firestore, CloudEvents 1.0, ports and adapters; P0 live |
| 2026-11-17 | Credentials and tool config stay inside the repo folder (`.factory-secrets/`) |
| 2026-11-17 | PRs only; nothing pushes to shared branches; base branch `main`, a per-product console setting |
| 2026-11-17 | Compliance agent advisory, rules start empty, edited in a versioned prompt manager |
| 2026-11-17 | Agents on GKE use Claude via Vertex AI + Workload Identity; no Claude secret in pods; subscription token only for the owner's interactive use |
| 2026-11-17 | Cluster and console reachable only through the main node; no public endpoints |
| 2026-11-17 | Prompt manager: git adapter first, MLflow next; skills as a Claude Code plugin marketplace |
| 2026-11-17 | Compliance agent is a plain Python process, not an Archon workflow |
| 2026-11-17 | Console is served from the main node (not Cloud Run with Google sign-in) |
| 2026-11-21 | No Vertex AI for now; agents use Claude as the project does. Hosting is Docker on factory-1 and sandbox VMs; GKE parked. Install, deploy, process control and agent chains are managed from the console |

## 14. Open questions

| Question | Owner | Blocks |
|---|---|---|
| Claude for unattended agents: project login or an API key per agent | owner | VM-hosted agents (R5) |
| Factory GitHub identity (two GitHub Apps recommended) | owner | PR comments/checks (OC8), builder identity |
| Remove factory-1's public IP | owner | OB7 |
| Holdouts outside the repo; what to do with `d58edec` on the converter's `main` | owner | N2, first real lap |
| Can the pinned Archon run workflows kept outside the product repo | check | the engine as a deployable component on other hosts |
| Console identity per operator (shared tunnel today) | design | audit by person (UI-2) |
