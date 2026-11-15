# Factory event types (v1)

Every event is a CloudEvents 1.0 event whose `type` is one of the names below
(`dev.factory.<entity>.<verb>`). The list is closed: a new type is added here, with its
`data` schema in `data/`, before anything publishes it. Renaming or removing a type is a
new version (`…v2`), never an edit.

`subject` names what the event is about, so a consumer can filter without parsing
`data`. The source column is where today's adapter takes it from.

## Runs and nodes (EngineAdapter)

| Type | Subject | Meaning | Source today (Archon) |
|---|---|---|---|
| `dev.factory.run.started` | `run/{run}` | a workflow run began | `workflow_started` |
| `dev.factory.run.completed` | `run/{run}` | finished; `data.outcome` is the workflow's verdict | `workflow_completed` |
| `dev.factory.run.failed` | `run/{run}` | stopped on an error; `data.failure_class` | `workflow_failed` |
| `dev.factory.run.cancelled` | `run/{run}` | cancelled by a person or the engine | `workflow_cancelled` |
| `dev.factory.run.paused` | `run/{run}` | waiting (CI, usage limit, approval) | run status `paused` |
| `dev.factory.run.resumed` | `run/{run}` | resumed; `data.attempt` incremented | resumed segment |
| `dev.factory.run.abandoned` | `run/{run}` | the scheduler gave up on a silent run | factory scheduler sweep |
| `dev.factory.run.lost` | `run/{run}` | the engine vanished while it ran | mission control (heartbeat stopped) |
| `dev.factory.graph.declared` | `run/{run}` | the run's nodes and edges | workflow definition at start |
| `dev.factory.node.started` | `run/{run}/node/{node}` | a step began; agent, model, sandbox | `node_started` |
| `dev.factory.node.completed` | `run/{run}/node/{node}` | a step finished | `node_completed` |
| `dev.factory.node.failed` | `run/{run}/node/{node}` | a step failed; `data.failure_class` | `node_failed` |
| `dev.factory.node.skipped` | `run/{run}/node/{node}` | a step's condition was false | `node_skipped` |
| `dev.factory.node.reused` | `run/{run}/node/{node}` | a rerun reused a prior success | `node_skipped_prior_success` |
| `dev.factory.node.suspended` | `run/{run}/node/{node}` | a step paused mid-way | `node_suspended` |
| `dev.factory.iteration.started` | `run/{run}/node/{node}/iteration/{n}` | a loop iteration began | `loop_iteration_started` |
| `dev.factory.iteration.completed` | same | a loop iteration finished | `loop_iteration_completed` |
| `dev.factory.iteration.failed` | same | a loop iteration failed | `loop_iteration_failed` |
| `dev.factory.handoff.passed` | `run/{run}/edge/{from}/{to}` | output or artifact passed between steps | derived: downstream `node_started` + upstream artifact |
| `dev.factory.artifact.created` | `run/{run}/node/{node}` | a step produced an artifact (pointer, not content) | `workflow_artifact` |
| `dev.factory.tool.activity` | `run/{run}/node/{node}` | rollup of agent tool calls since the last one | `provider_event` (rolled up by the bridge) |
| `dev.factory.approval.requested` | `run/{run}/node/{node}` | a run waits for a person | `approval_pending` |
| `dev.factory.approval.resolved` | `run/{run}/node/{node}` | approved or rejected | approval resolution |
| `dev.factory.sandbox.started` | `run/{run}/sandbox/{id}` | a container or worktree came up | `container_lifecycle` |
| `dev.factory.sandbox.stopped` | `run/{run}/sandbox/{id}` | it went away | `container_lifecycle` |

## Engines

| Type | Subject | Meaning | Source today |
|---|---|---|---|
| `dev.factory.engine.heartbeat` | `engine/{engine}` | alive; slots, halted/paused, pin, bridge cursor | bridge, every 60 s |
| `dev.factory.engine.registered` | `engine/{engine}` | a machine asked to join | join agent (P3) |
| `dev.factory.engine.approved` | `engine/{engine}` | the owner approved it | dashboard (P3) |
| `dev.factory.engine.revoked` | `engine/{engine}` | its token was revoked | dashboard (P3) |
| `dev.factory.engine.halted` | `engine/{engine}` | launches blocked | `consumer.py halt` |
| `dev.factory.engine.unhalted` | `engine/{engine}` | launches allowed | `consumer.py unhalt` |
| `dev.factory.engine.auth_changed` | `engine/{engine}` | sign-in method added, removed, failed a test call (never the secret) | AgentAuth (P3) |

## Factory decisions (factory files)

| Type | Subject | Meaning | Source today |
|---|---|---|---|
| `dev.factory.schedule.fired` | `engine/{engine}` | a timer tick started a lap | `.factory/schedule.log` |
| `dev.factory.schedule.skipped` | `engine/{engine}` | a tick started nothing, and why | `.factory/schedule.log` |
| `dev.factory.gate.decided` | `workitem/{id}` | merge gate merged or held, with reasons | `.factory/ledger.jsonl` / gate record |
| `dev.factory.holdout.exposed` | `engine/{engine}` | the fence saw scenario text where it should not be (no text included) | `holdout-exposures.jsonl` |
| `dev.factory.deploy.completed` | `workitem/{id}` | the default branch is live at a revision | `archon-deploy` |
| `dev.factory.deploy.failed` | `workitem/{id}` | deploy or health read-back failed | `archon-deploy` |

## Work items and code (WorkTracker, CodeHost)

| Type | Subject | Meaning | Source today (GitHub) |
|---|---|---|---|
| `dev.factory.workitem.opened` | `workitem/{id}` | a work item exists | issue opened |
| `dev.factory.workitem.updated` | `workitem/{id}` | title, labels, priority, dependencies changed | issue edited / labeled |
| `dev.factory.workitem.claimed` | `workitem/{id}` | a lap claimed it | claim comment |
| `dev.factory.workitem.released` | `workitem/{id}` | the claim ended | claim removed / expired |
| `dev.factory.workitem.closed` | `workitem/{id}` | done or dropped | issue closed |
| `dev.factory.pullrequest.opened` | `change/{id}` | a change was proposed | PR opened |
| `dev.factory.pullrequest.updated` | `change/{id}` | new head commit | PR synchronize |
| `dev.factory.pullrequest.merged` | `change/{id}` | merged | PR merged |
| `dev.factory.pullrequest.closed` | `change/{id}` | closed unmerged | PR closed |
| `dev.factory.check.completed` | `change/{id}/check/{name}` | a CI check concluded | check run completed |
| `dev.factory.review.submitted` | `change/{id}` | a review verdict | PR review |

## Memory (MemoryStore)

| Type | Subject | Meaning | Source today |
|---|---|---|---|
| `dev.factory.memory.changed` | `memory/{product}/{doc}` | a knowledge document has a new version (pointer + hash, never holdout text) | git push to the app repo |

## Mission control

| Type | Subject | Meaning | Source |
|---|---|---|---|
| `dev.factory.problem.opened` | `problem/{id}` | a rule opened a problem | writer / scheduler rules |
| `dev.factory.problem.resolved` | `problem/{id}` | it cleared | same |
| `dev.factory.command.issued` | `engine/{engine}/command/{id}` | halt, unhalt, approve, rerun, assign | dashboard / coordinator (P3–P4) |
| `dev.factory.command.acknowledged` | `engine/{engine}/command/{id}` | the engine accepted or refused it | engine (P3) |
| `dev.factory.queue.updated` | `queue/{product}` | the coordinator's ordered queue and reasons | coordinator (P4) |

## Id formats

| Thing | Format | Example |
|---|---|---|
| work item | `tracker:{adapter}/{key}` | `tracker:github/shdmitry2000/file-to-markdown-convertor#14`, `tracker:jira/FAC-12` |
| change | `codehost:{adapter}/{repo}#{n}` | `codehost:github/shdmitry2000/file-to-markdown-convertor#16` |
| run | the engine's run id | `8f3c21…` |
| event `id` | `{source-kind}:{source id}` | `archon:7f3c…`, `github:delivery-…`, `ledger:sha256-…` |
