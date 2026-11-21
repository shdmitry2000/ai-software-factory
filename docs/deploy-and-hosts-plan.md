# Hosts, deployment and agent chains: plan

How the factory installs, deploys and runs its parts (agents and system components)
on the main node and on other servers in the sandbox, all driven from the console.

Status: 2026-11-21. R1–R5 and R7–R9 done and live; R6 (chain triggers) built 2026-11-21: deploying a component that `consumes` events creates its filtered subscription. The console creates VM hosts, deploys to them, streams logs, upgrades, rolls back and restores the fleet (drill: §10). Replaces the GKE-first approach of Stage O-B and the
old Stage R (fleet). Architecture: [architecture.md](architecture.md). Console:
[console-ui-plan.md](console-ui-plan.md).

## 1. Decision (2026-11-21)

- **No Vertex AI for now.** Agents use Claude the same way the rest of the project
  does: the project's Claude login, delivered to the machine that runs the agent.
  Vertex stays a possible per-agent option later (`llm: vertex`).
- **Hosting is servers and Docker.** Every part runs as a Docker container on the
  main node (factory-1) or on another server in the same sandbox project.
- **GKE is parked.** `bin/gke/` and the k8s manifests stay in the repo, unused. The
  cluster is not recreated after the weekend sweep.
- **The console is the one place to manage it.** Add a server, install it, deploy an
  agent to it, start, stop, restart, upgrade, roll back, read its logs, and chain
  agents together. Every one of these is a command event, as everywhere else.

## 2. Concepts

| Concept | What it is | Where it is kept |
|---|---|---|
| **Host** | a machine that runs containers. `local` = factory-1 itself. `vm` = a VM in the sandbox the console creates and installs. Later: `ssh` (an existing server outside the sandbox), `gke` | desired: `deploy/hosts.yaml` (git, changed by PR); actual: Firestore `hosts/{id}` |
| **Component** | anything deployable: an agent (compliance, later others) or a system part (bridge, later the engine) | `deploy/components/<name>.yaml` |
| **Deployment** | one component, at one image digest, on one host, with its settings and replica count | desired: `deploy/deployments.yaml`; actual: `deployments/{id}` |
| **Chain** | agents linked by events: a component *consumes* event types and *emits* others. The chain is the graph of those declarations | derived from the component files |

A component file:

```yaml
name: compliance
image: me-west1-docker.pkg.dev/<project>/mission-control/agent   # built on factory-1, pulled by digest
command: [python, -m, agents.compliance, --bus, pubsub, --store, firestore]
llm: project            # project | api_key | none   (vertex later)
consumes:               # what starts its work (Pub/Sub subscription with a filter)
  - type: dev.factory.pullrequest.opened
  - type: dev.factory.pullrequest.updated
emits:
  - dev.factory.compliance.reported
  - dev.factory.agent.heartbeat
settings:               # non-secret, shown and edited in the console
  interval: 600
  model: claude-haiku-5-5
resources: {cpu: "0.5", memory: 512Mi}
```

A deployment line:

```yaml
- component: compliance
  host: local                 # or vm-agents-1
  digest: sha256:…            # pinned; upgrade = new digest, rollback = old one
  replicas: 1
  products: [file-to-markdown-convertor]
```

## 3. Security (the main-node rule still holds)

- **Only the main node deploys.** The console and the command handler run on
  factory-1; it alone holds the SSH access to other hosts. Nothing is deployed from
  the Mac or from a browser.
- **Hosts in the sandbox have no public IP.** Firewall: SSH (tcp:22) to agent hosts
  only from VMs tagged `factory` (rule `allow-ssh-factory-to-agents`, target tag
  `factory-agent`); no other inbound, not even IAP. Egress through Cloud NAT
  (`factory-router`/`factory-nat`, made by `bin/gcp/create-factory-vm.sh`). OS Login stays enforced: `factory-vm` gets
  `roles/compute.osAdminLogin` on agent hosts only.
- **Containers have no inbound ports.** They talk out to Pub/Sub, Firestore, GitHub
  and Anthropic, nothing else.
- **Host identity.** Agent hosts run as a new service account `factory-agent`:
  Pub/Sub publisher on `factory-events`, Firestore user (made by
  `mission-control/infra/setup.sh`). No keys.
- **The LLM credential.** Copied by the main node (from `/root/.factory-env`, over
  SSH stdin for other hosts) into `/etc/factory/<component>.env`, mode 600, owned by
  the container's user, and mounted read-only at `/run/factory/llm.env`; the container
  gets only `FACTORY_ENV_FILE`. Not `docker --env-file`, which would copy the value
  into `docker inspect`. Never in an image, git, an event, the console page or a log line. The
  console shows only *which* method an agent uses (`project`, `api_key`).
- **Images.** Built on factory-1 from its checkout and run by image id. A VM host gets
  the image from factory-1 itself (`docker save | ssh docker load`), so VMs need no
  registry access; the last 5 builds per component stay on factory-1 for roll back.
- **Names and grants.** VM hosts are named `agent-*`. factory-vm's
  `compute.instanceAdmin.v1` and `compute.osAdminLogin` grants carry IAM conditions
  that limit them to instances and disks named `agent-*`, so the handler cannot touch
  factory-1 or other workloads. At most 3 VM hosts (handler setting).

## 4. How a deploy runs

```
 operator: Deploy "compliance" to vm-agents-1
  ─▶ console (factory-1) ─▶ dev.factory.command.issued {deployment.apply, who, reason}
  ─▶ command handler (factory-1) ─▶ AgentRuntime adapter for the host kind
       local:  docker on factory-1
       vm:     docker over SSH (DOCKER_HOST=ssh://…) through the internal IP
  ─▶ steps, each one an event dev.factory.deployment.step {seq, step, ok, detail}:
       pull image by digest → write env file → create Pub/Sub subscriptions (consumes)
       → run container (restart unless-stopped) → wait for first heartbeat
  (command.acknowledged comes first: accepted, or refused with the reason)
  ─▶ dev.factory.deployment.changed {ok, error, instances}
  ─▶ console shows progress live; the agent appears on the Agents screen
```

**Install a new host** (`host.create`, then `host.install`): the handler creates a VM
in the sandbox (no public IP, SA `factory-agent`, the SSH firewall rule), then runs one
idempotent bootstrap over SSH: Docker, the Artifact Registry login helper, a
`factory` systemd target, a disk check. Done when the host reports its first health
heartbeat (CPU, memory, disk, Docker version).

**Process controls**: start, stop, restart, scale replicas, upgrade (new digest),
roll back (previous digest), logs (last N lines, streamed while the panel is open),
remove. Each is a command; each result is an event.

## 5. After the weekend sweep

The sweep deletes factory-1 and every agent VM. Firestore keeps `hosts/{id}.desired`
(machine type, zone) and `deployments/{id}.desired` (replicas, products, digest), and
the desired state in git survives. Console changes win over git until "Reset to git";
the Deploy screen marks each difference. After `bin/gcp/rebuild.sh`
brings factory-1 back, the console's **Restore** button (command `fleet.restore`)
recreates the VM hosts, reinstalls them and reapplies every deployment at its pinned
digest; a pinned image the rebuilt factory-1 no longer has is rebuilt from the
checkout once. Agents read their progress back from Firestore, so nothing is reported
twice.

## 6. AgentRuntime port

`mission_control/ports.py`, with contract tests every adapter must pass:

```
hosts() -> list[Host]                    create_host(spec) -> Host
install(host) -> Iterator[Step]          remove_host(host)
apply(deployment) -> Iterator[Step]      remove(deployment)
start / stop / restart(deployment)       scale(deployment, replicas)
status() -> list[Instance]               logs(deployment, tail, follow) -> Iterator[str]
```

Plus `health(host)` (CPU, memory, disk, Docker; a `host.heartbeat` every 60 s) and
`follow_logs(deployment)`.

Adapters: `fake` (tests), `docker-local`, `docker-ssh` (docker over SSH from the main
node), `fleet` (routes by host: `local` or a VM; creates VMs through `gce.py`, the
gcloud CLI as factory-vm). Later: `gke` (from the parked scripts), `ssh` for servers
outside the sandbox.

## 7. Console screens added

| Screen | Shows | Actions |
|---|---|---|
| **Hosts** | each host: kind, state, CPU/memory/disk, Docker version, what runs on it | add VM, install, drain, remove, restore all |
| **Deploy** | component catalog; deployments with digest, host, replicas, state, last change | deploy, upgrade, roll back, start/stop/restart, scale, logs, remove |
| **Chains** | graph: event type → component → event type, live counts per edge | add a component to the chain (pick trigger events and a host) |
| **Agents** (exists) | running instances and their heartbeats | drain |

Wireframes are in [console-ui-plan.md](console-ui-plan.md#hosts-and-deploy).

## 8. Build steps (Stage R, new)

| # | Step | Done when |
|---|---|---|
| R1 ✅ | AgentRuntime port, `fake` adapter, contract tests | contract tests pass on the fake |
| R2 ✅ | `docker-local` adapter; the compliance agent runs as a container on factory-1 with `llm: project` | heartbeat in `agents/` every 60 s; `no_rules` reports for open PRs; no credential in `docker inspect` output beyond the env-file path |
| R3 ✅ | `deploy/components/*.yaml`, `deploy/hosts.yaml`, `deploy/deployments.yaml`: schema, loader, tests | the compliance component and the `local` host load; bad files are refused |
| R4 ✅ | Command handler: `deployment.apply/start/stop/restart/remove`, progress events, schemas | unit tests on the fake; a real apply on factory-1 shows each step as an event |
| R5 ✅ | `fleet` + `docker-ssh` adapters, `gce.py`: VM without public IP, SA `factory-agent`, SSH only from factory-1 | a throwaway VM is created, installed, runs compliance, is removed; SSH from the Mac to it fails |
| R6 ◐ | Chains: `consumes` creates filtered Pub/Sub subscriptions at deploy (`mission_control/triggers.py`: one `factory-trig-{component}--{host}` per deployment, filter on the `type` and `product` attributes the bus sets, pull granted to the host's service account, deleted on remove); agents start on events as well as on a timer. Live 2026-11-21: `compliance@local` redeployed with its trigger; a `pullrequest.updated` check event woke the agent within 15 s, nothing else did in 5 min; the new-PR check is next | opening a PR triggers a compliance report within 1 minute (the poller sends `pullrequest.opened` within about a minute) |
| R7 ✅ | Logs: tail and follow through the handler (root-only socket `/run/mission-control-handler/logs.sock`, credential values masked) | the console shows the last 200 lines and new lines live |
| R8 ◐ | `fleet.restore` after the sweep (simulated drill passed; the real weekend drill is next) | weekend drill: one VM host and its agents back with one click, no duplicate reports |
| R9 ✅ | Upgrade and roll back by digest; keep the last 5 digests per component | a rollback returns the previous digest and the agent keeps working |

Console steps that go with these: UI-3 in [console-ui-plan.md](console-ui-plan.md).

## 9. Open questions

| Question | Owner |
|---|---|
| Claude for unattended agents on extra hosts: the project login, or an API key per agent? Anthropic's terms tie subscription logins to the owner's own use; an API key is the clean choice for agents that run on their own | owner |
| Servers outside the sandbox (`ssh` host kind): needed, and how they reach Pub/Sub | owner |
| Spend limit per host (VM size and count cap shown in the console) | owner |
| Does the engine (Archon) itself become a deployable component on another host | later, after R5 |

## 10. Drill, 2026-11-21 (all from the console's endpoints)

| Step | Command | Result |
|---|---|---|
| Add VM host | `host.create agent-1` (e2-small) | created at 10.208.0.51, no public IP; SSH, Docker 29.1.3, `/etc/factory`, disk 17% |
| Deploy to it | `deployment.apply compliance@agent-1` at factory-1's image | image shipped over SSH, credential file 600 (uid 10001), container running; heartbeat from `docker:agent-1/…`; `docker inspect` shows only the file path |
| Reach it from outside | IAP SSH from the Mac | times out: the firewall admits only factory-1 |
| Simulated sweep | VM deleted with gcloud behind the factory's back | Hosts showed agent-1 unreachable |
| Restore all | `fleet.restore factory-1` | agent-1 recreated (new IP), installed, compliance redeployed at the same digest; factory-1's deployment unchanged |
| Upgrade, roll back | `deployment.apply` with `build`, then with the old digest | new image built from HEAD and running; roll back returned the old digest, agent running |
| Remove host | `host.remove agent-1` | containers and VM deleted; deployment marked removed; nothing left in Needs you |
