# Console (operator UI): plan

One console for the whole factory: every product, agent, run and report, the
settings that steer them, and the buttons that act on them. It replaces the earlier
"dashboard on Cloud Run behind Google sign-in" plan, because the owner's rule
(2026-11-17) is that **everything is used only through the main node**.

Status: 2026-11-21. UI-0 and UI-1 done and live on factory-1 (`bin/gcp/console.sh`). Architecture: [architecture.md](architecture.md) §9.
Data and events: [mission-control-design.md](mission-control-design.md). Build steps
are below and mirrored in [mission-control-plan.md](mission-control-plan.md) (Stage F).

## 1. Goals

1. **See everything at a glance**: products, agents, running and past runs, problems,
   compliance findings, spend.
2. **Change settings with a record**: base branch, compliance mode and rules label,
   pool sizes, caps. Every change is an event with who, when, old and new value.
3. **Act**: halt and unhalt, rerun, approve, add or drain an agent, move a prompt label.
4. **Drill down**: from a product to its PRs, from a PR to its laps and compliance
   reports, from a lap to its graph, nodes and agent messages.
5. **Secure by construction**: reachable only through the main node; no public URL.

Non-goals: replacing Archon's own run view (linked instead); editing code; reading
holdout text (never shown).

## 2. Access model

```
 operator laptop                         factory-1 (main node)
 ┌──────────────────┐   IAP + OS Login   ┌──────────────────────────────────────┐
 │ browser          │   (Google account, │ sshd ── forwards to ──▶ unix socket   │
 │ localhost:8080 ──┼── SSH -L 8080: ───▶│         /run/factory-console/sock     │
 │                  │   socket path)     │           │ (group factory-operators) │
 └──────────────────┘                    │           ▼                           │
   bin/gcp/console.sh opens the tunnel   │  console server (FastAPI)             │
   and the browser                       │   reads Firestore (factory-vm SA)     │
                                         │   publishes commands                  │
                                         │   (Archon UI: same SSH, -L 3090)      │
                                         └──────────────────────────────────────┘
```

- **No TCP port is exposed**, not even to the VPC: the console listens on a unix
  socket. Only users who can SSH to factory-1 through IAP (an IAM grant) and who are
  in the `factory-operators` group can reach it.
- **Operator identity without passwords**: sshd connects to the socket as the
  logged-in OS Login user, so the server reads the peer's uid (`SO_PEERCRED`) and maps
  it to the Google account. Every command event carries that identity. (Spike UI-0.3
  confirms this; fallback: a per-session token printed by `console.sh`.)
- **Archon's console** is forwarded by the same SSH connection (`-L 3090:127.0.0.1:3090`)
  instead of being proxied under `/archon/` (its web app uses root paths, so a sub-path
  proxy breaks it). The IAP firewall rule for tcp:3090 is removed: only SSH is open.
- **Who may use it**: the socket is connectable by local users, and the app admits only
  root and members of `factory-operators` (checked per request from `SO_PEERCRED`).
  `console.sh` adds the signed-in OS Login admin to the group.
- The console is **stateless**: everything it shows comes from Firestore, so the
  weekly rebuild of factory-1 loses nothing.

## 3. Technology

| Choice | Why |
|---|---|
| **FastAPI** server in `mission-control/console/`, Python like the rest | reuses the ports (StateStore, EventBus, PromptRegistry, CodeHost) and their adapters |
| **Server-rendered HTML + htmx**, live updates over **SSE** | no SPA build chain; the server holds Firestore listeners and pushes changed fragments |
| **Cytoscape.js** (vendored, pinned) for run graphs | DAGs with fan-out and joins (design goal 3) |
| Static assets vendored in the repo | works without the browser loading anything from the internet |
| systemd unit `factory-console` | started by the VM setup; restarts on failure |

Alternative kept open: a React SPA over the same JSON API (`/api/…`) if the views
outgrow htmx. The API is designed first either way (OpenAPI in `mission-control/schema/`).

## 4. Information architecture

```
 ┌───────────────────────────────────────────────────────────────────────────┐
 │ Factory console    [Overview] Projects  Agents  Hosts  Deploy  Chains     │
 │                     Runs  Workflows  Compliance  Problems  Memories       │
 │                     Prompts  Audit                                        │
 │                                         operator: owner@…  ● live  ⏸ halt │
 └───────────────────────────────────────────────────────────────────────────┘
```

| Screen | Answers | Reads | Actions | Stage |
|---|---|---|---|---|
| **Overview** | is everything healthy right now | rollups of all collections | halt all | UI-1 |
| **Projects** | each product: open PRs, last lap, problems, compliance, git freshness | `products`, `compliance`, `problems` | edit settings (base branch, compliance mode, rules label, cap) | UI-1 / UI-2 |
| **Project detail** | PRs with laps and reports; settings history | `products/{p}`, `work_items`, `compliance` | rerun, approve merge | UI-2 / UI-4 |
| **Agents** | every running instance: host, state, current work, model, LLM method, spend | `agents` | drain | UI-1 / UI-3 |
| **Hosts** | factory-1 and every agent VM: state, CPU/memory/disk, Docker, what runs there | `hosts` | add VM, install, drain, remove, restore all | UI-3 |
| **Deploy** | components and their deployments: digest, host, replicas, state | `deployments`, `deploy/` | deploy, upgrade, roll back, start/stop/restart, scale, logs, remove | UI-3 |
| **Chains** | which events start which agent, and what each emits | component files, event counts | add a component to a chain | UI-3 |
| **Runs** | history; retry chains; filters | `runs` | rerun, cancel | UI-1 / UI-4 |
| **Run detail** | live graph, node status, handoffs, tool activity | `runs/{r}`, `nodes` | open in Archon | UI-4 |
| **Workflows** | definitions, their runs, per-node agent conversation | `workflows`, `runs/{r}/messages` | – | UI-5 |
| **Compliance** | every PR head's advisory report, rules version, findings, cost | `compliance` | re-review a PR | UI-1 |
| **Problems** | what needs a human | `problems` | acknowledge, resolve | UI-2 |
| **Memories** | mission, decisions, conventions, lessons with history | `memory_index` | propose change (opens PR) | UI-6 |
| **Prompts & Skills** | versions, labels, diffs; which runs used which version | PromptRegistry, SkillRegistry | new version, move label (opens PR with git adapter) | UI-6 |
| **Audit** | who did what, when | `commands`, settings events | – | UI-2 |

## 5. Screens (wireframes)

### Overview

```
 ┌ Health ──────────────────────────────────────────────────────────────────┐
 │ Engines 1/1 alive   Agents 2 running (hosts: 1)       Problems 0 open     │
 │ Laps today 0        Compliance reports today 3        Spend today $0.00   │
 └──────────────────────────────────────────────────────────────────────────┘
 ┌ Running now ────────────────────────┐ ┌ Needs you ─────────────────────────┐
 │ (none)                              │ │ ⚠ vm-agents-1 disk 91%              │
 │                                     │ │ ⚠ GitHub identity not decided       │
 └─────────────────────────────────────┘ └────────────────────────────────────┘
 ┌ Recent ──────────────────────────────────────────────────────────────────┐
 │ 12:01  compliance  converter #3  no_rules  rules v1   $0.00               │
 │ 11:40  run         archon-holdouts  completed  6 m                        │
 └──────────────────────────────────────────────────────────────────────────┘
```

### Projects → product settings

```
 file-to-markdown-convertor          github: shdmitry2000/file-to-markdown-convertor
 ┌ Settings ───────────────────────────────────────────────── [Edit] ┐
 │ Base branch        main            changed 2026-11-17 by owner      │
 │ Compliance         advisory · rules compliance/rules @ production (v1, empty) │
 │ Daily cap          $1.00                                             │
 │ Pools              compliance (1..3), builders (1..1)                │
 └──────────────────────────────────────────────────────────────────────┘
 ┌ Open PRs ────────────────────────────────────────────────────────────┐
 │ #3 Factory mission draft  → main  head f9b6bff  compliance: no_rules  │
 │    laps: –   checks: –   merge: on hold                               │
 └──────────────────────────────────────────────────────────────────────┘
 Edit dialog: field, new value, reason (required) → [Preview event] [Apply]
```

### Agents

```
 ┌──────────────────────────────────────────────────────────────────────────┐
 │ agent              host            state  current work      model   llm  │
 │ compliance-1       factory-1       idle   –                 haiku   proj │
 │ engine             factory-1       busy   lap 41 build      opus    proj │
 └──────────────────────────────────────────────────────────────────────────┘
```

### Hosts and deploy

Design: [deploy-and-hosts-plan.md](deploy-and-hosts-plan.md).

```
 Hosts                                         [+ Add VM]  [Restore all]
 ┌──────────────────────────────────────────────────────────────────────────┐
 │ host          kind   state      cpu  mem   disk  docker  runs            │
 │ factory-1     local  healthy    34%  61%   48%   27.3    engine, bridge, │
 │                                                          compliance      │
 │ vm-agents-1   vm     installing  –    –     –     –      step 3/5 docker │
 └──────────────────────────────────────────────────────────────────────────┘

 Deploy                                                     [+ Deploy]
 ┌──────────────────────────────────────────────────────────────────────────┐
 │ component   host        digest        replicas  state    llm             │
 │ compliance  factory-1   sha256:3f9c…  1         running  project         │
 │   [Restart] [Stop] [Scale] [Upgrade ▾] [Roll back] [Logs] [Remove]       │
 └──────────────────────────────────────────────────────────────────────────┘
 Deploy dialog: component ▾  host ▾  digest ▾ (latest 5)  replicas  settings
                reason ______  [Deploy]  → progress: pull ✓ env ✓ subs ✓ run … heartbeat

 Chains
   pr.opened ─┐
   pr.updated ┴─▶ compliance ─▶ compliance.reported ─▶ (add next agent +)
```

### Run detail (live)

```
 run 8f3c21 · archon-sdlc-loop · converter #14 · attempt 2 (rerun of 7a01…)
   pick ──▶ build ──┬──▶ validate ───┐
                    ├──▶ app-check ──┼──▶ review ──▶ merge-gate ──▶ deploy
                    └──▶ holdout ────┘      ● running: reviewer-b (sonnet)
 node build: 6m12s · 41k tokens · tools: Edit 12, Bash 7, Read 30   [messages ▸]
```

### Compliance

```
 product     PR  head     base  rules      status    findings  cost   reported
 converter   #3  f9b6bff  main  v1 (0)    no_rules  –         $0.00  12:01
 [rules: compliance/rules @ production → v1]  [view versions] [re-review]
```

## 6. Actions and how they execute

Every action is a command, never a direct write:

```
 button ─▶ console server: validate + who ─▶ dev.factory.command.issued (factory-commands)
        ─▶ handler on factory-1 ─▶ acknowledged / refused
        ─▶ result event (product.updated, engine.halted, pool.scaled …) ─▶ projections
```

| Action | Command | Executed by | Result event |
|---|---|---|---|
| Change base branch / compliance mode / cap | `product.update {field, old, new, reason}` | handler (checks old value matches) | `dev.factory.product.updated` |
| Halt / unhalt engine | `engine.halt` / `engine.unhalt` | handler → `consumer.py halt` | `engine.halted` / `engine.unhalted` |
| Rerun / cancel run | `run.rerun` / `run.cancel` | handler → Archon | `run.started` (chained) / `run.cancelled` |
| Approve merge | `approval.resolve` | handler → Archon | `approval.resolved` |
| Add VM host / install / remove | `host.create` / `host.install` / `host.remove` | handler → AgentRuntime (`gce-vm`) | `host.created`, `deployment.step`, `host.installed` |
| Deploy / upgrade / roll back | `deployment.apply {component, host, digest, replicas}` | handler → AgentRuntime (`docker-local`, `docker-ssh`) | `deployment.step` per step, `deployment.changed` |
| Start / stop / restart / scale / remove | `deployment.start` … `deployment.remove` | handler → AgentRuntime | `deployment.changed`, `agent.heartbeat` |
| Logs | `deployment.logs {tail, follow}` (read-only, not stored) | handler streams to the console | – |
| Restore after the sweep | `fleet.restore` | handler: recreate hosts, reinstall, reapply | `host.installed`, `deployment.changed` |
| Drain an agent | `agent.drain` | handler | `agent.heartbeat` (state draining) |
| New prompt version / move label | `prompt.propose {name, text}` / `prompt.label {name, label, version}` | PromptRegistry adapter (git: opens a PR; service: needs approver) | `prompt.version.created`, `prompt.label.moved` |
| Re-review a PR | `compliance.rereview {product, pr}` | compliance agent | `compliance.reported` |
| Propose memory change | `memory.propose` | MemoryStore (opens a PR) | `memory.changed` after merge |

Rules: destructive actions ask to confirm; every command needs a reason; settings
changes are optimistic-locked (the old value must match); the console never writes to
Firestore itself.

**Base branch as a setting.** `products/<product>.yaml` seeds it. A console change
emits `product.updated`; the `products` projection holds the current value; the engine
and agents read it from there at the start of each lap or tick. A change applies to
the next lap and never to a PR already open (its base stays what it was opened with).

## 7. Build steps

Each step ends with a check anyone can run. "API" means a JSON route the page uses
and tests call.

### UI-0 Shell and access

| # | Step | Done when |
|---|---|---|
| UI-0.1 ✅ | `mission_control/console/` FastAPI app, layout, nav, health route; runs locally against memory adapters (`--demo`) | `uvicorn` locally shows the empty shell; unit test for `/api/health` |
| UI-0.2 ✅ | systemd unit `factory-console` on factory-1, unix socket `/run/factory-console/sock`, group `factory-operators` | `curl --unix-socket` on the VM returns health; nothing listens on TCP |
| UI-0.3 ✅ | Spike: operator identity from `SO_PEERCRED` through `ssh -L` to the socket; map uid → OS Login user | the page header shows the real operator. **Works**: the OS Login user and its groups arrive by peer credentials; no fallback needed |
| UI-0.4 ✅ | `bin/gcp/console.sh`: IAP SSH tunnel to the socket, opens the browser | from the Mac the console opens; from another machine without IAP grant it does not |
| UI-0.5 ✅ | Archon UI forwarded over the same SSH connection; firewall rule for tcp:3090 removed | Archon answers on localhost:3090 through the tunnel; no 3090 rule exists |
| UI-0.6 ✅ | SSE live channel: the server polls the newest event (only while a page is open) and pushes fragments | a new event appears on the page within 2 s without reload (measured 0–1 s) |

### UI-1 Status, read-only (Stage F + Q views)

| # | Step | Done when |
|---|---|---|
| UI-1.1 ✅ | Overview health tiles and "needs you" list | numbers match Firestore by hand count |
| UI-1.2 ✅ | Agents view (from `agents`, `engines`) | factory-1 and the compliance container both listed live |
| UI-1.3 ✅ | Projects view (from `products/*.yaml` until the Q1 projection, `compliance`, `runs`) | the converter with its open PRs and reports |
| UI-1.4 ✅ | Compliance view | every report with rules version, findings, cost |
| UI-1.5 ✅ | Runs view: history, filters, retry chains | a rerun shows under its first attempt |
| UI-1.6 ✅ | Problems view; rules derived on read in `console/views.py` (silent engine/agent, halted engine, deployment down/failed, failed or stuck run, compliance error/capped/high) | a forced failure shows and clears (deployment stop → start, 2026-11-21) |

### UI-2 Settings, commands and audit (Stage M + N1)

| # | Step | Done when |
|---|---|---|
| UI-2.1 | Command events and the factory-1 command handler (`product.update`, `engine.halt`) with schemas | unit tests: optimistic lock, refused on stale old value |
| UI-2.2 | Product settings editor (base branch, compliance mode, rules label, cap) | changing base branch emits `product.updated`; next lap uses it; open PRs unchanged |
| UI-2.3 | Halt / unhalt button | engine halts; audit shows operator and reason |
| UI-2.4 | Audit view | every command with who, when, reason, result |
| UI-2.5 | Problems: acknowledge / resolve | state change recorded as an event |

### UI-3 Hosts, deploy and chains (Stage R, [deploy-and-hosts-plan.md](deploy-and-hosts-plan.md))

| # | Step | Done when |
|---|---|---|
| UI-3.1 | Hosts screen (read-only), from `hosts` | factory-1 listed with health and what runs on it |
| UI-3.2 | Deploy screen: deployments with digest and state; start/stop/restart/scale | restarting compliance shows each step live and the audit entry |
| UI-3.3 | Deploy dialog: component, host, digest, replicas, settings, reason | deploying compliance to factory-1 from the console works end to end |
| UI-3.4 | Add VM host + install with live progress | a new VM reaches healthy; compliance deployed to it |
| UI-3.5 | Logs panel (tail + follow) | last 200 lines, then new lines live |
| UI-3.6 | Upgrade / roll back by digest | a rollback restores the previous digest |
| UI-3.7 | Chains graph from component files, event counts per edge | the compliance chain shows with today's counts |
| UI-3.8 | Restore all (after the sweep) | weekend drill passes with one click |

### UI-4 Live runs (Stage H)

| # | Step | Done when |
|---|---|---|
| UI-4.1 | Run detail graph (Cytoscape, live node colours) | a fan-out shows parallel running nodes |
| UI-4.2 | Node panel: times, tokens, tool rollup, handoffs | edge click shows what was handed over |
| UI-4.3 | Rerun / cancel / approve buttons | a rerun starts and chains |

### UI-5 Workflows and messages (Stage S)

| # | Step | Done when |
|---|---|---|
| UI-5.1 | Workflow catalog and its runs | pinned and project workflows listed |
| UI-5.2 | Per-node conversation (dispatch → tools → reply), large bodies from blobs | a lap's messages readable; holdout agents show metadata only |

### UI-6 Memories, prompts and skills (Stages U, T)

| # | Step | Done when |
|---|---|---|
| UI-6.1 | Memories: read, history, diff; propose change → PR | a PR opens against the base branch |
| UI-6.2 | Prompts: versions, labels, diff; which reports/runs used each version | compliance rules v1 shown with its reports |
| UI-6.3 | Rules editor: new version + move label (git adapter: opens a PR) | merging the PR changes the next report's rules version |
| UI-6.4 | Skills: marketplace versions per lap | a run lists its skill versions |

## 8. Order with the rest of the plan

```
 R1–R2 (compliance agent as a container on factory-1) → OC2–OC4
   → UI-0 → UI-1 (+ Q1–Q5 data) → UI-2 (+ N1 settings, M1)
   → R3–R9 + UI-3 (hosts, deploy, chains) → H + UI-4 → S + UI-5
   → I/U/T + UI-6 → E (first real lap)
```

UI-0 and UI-1 can start in parallel with R1–R2: they read collections that already
exist (`runs`, `nodes`, `engines`) and the new ones (`agents`, `compliance`).

## 9. Open questions

- Operator identity: confirm `SO_PEERCRED` through `ssh -L` (UI-0.3).
- More operators later: OS Login + IAM grants per person; the `factory-operators`
  group on factory-1 is managed by the VM setup script.
- Should the console ever be reachable when factory-1 is down (weekend)? Today: no;
  history stays in Firestore and is visible again after rebuild.
