# Prompt manager and skill manager: design

Two small services that version, label and serve the **prompts** and **skills** agents
use, so a change to what an agent is told is reviewed, testable, pinned per run and
reversible, like code. They are designed to stand alone: their own APIs, storage and
events, with no dependency on mission control or on the factory. Mission control is one
client; an engine is another; a person with `curl` is a third.

Status: design, 2026-11-17. Build steps: Stage T of [mission-control-plan.md](mission-control-plan.md).

## Goals

1. **Every prompt and skill has versions**, immutable once created, with who, when, why.
2. **Labels pick the version per environment** (`production`, `staging`, a canary), so
   rollout and rollback are a label move, not a deploy.
3. **Runs are reproducible**: an engine resolves everything it needs once at lap start
   into a **lock**, and a rerun of that lap uses the same lock.
4. **Evidence before promotion**: a version can carry links to eval results, and moving
   the `production` label can require them.
5. **Swappable**: the services sit behind ports, so an existing product (Langfuse,
   MLflow, a plain git repo, an OCI registry) can replace either one.

## Non-goals

- **Not an eval platform.** It stores links to eval runs and their verdicts; running
  evals belongs elsewhere.
- **Not a way to change the pinned Archon workflows.** Their command prompts are
  versioned by the Archon pin and must not be forked. The prompt manager can **import
  them read-only** (label `archon-pin-<sha>`) so they can be browsed and diffed, nothing
  more.
- **Not a secret store.** Prompts and skills must not contain credentials; creation
  rejects anything a secret scanner flags.

## What each manages

| Kind | Examples | Shape |
|---|---|---|
| **Prompt** | the compliance agent's review prompt; a project-level workflow node prompt; a Claude Code subagent definition (`.claude/agents/*.md`); a system prompt fragment | text template with declared variables, plus optional model hints |
| **Skill** | a Claude Code skill (folder with `SKILL.md` and resources/scripts); a Codex/other agent equivalent | a bundle (tar.gz), content-addressed, with a manifest |

## Data model

```
Namespace     org | org/product                  e.g. "factory", "factory/file-to-markdown-convertor"
Prompt        {namespace, name, kind: system|node|subagent|fragment, description, owners[]}
PromptVersion {version: int (1,2,3…), sha256, template, variables: JSON Schema,
               hints: {tier, effort, model?, max_tokens?}, frontmatter?: map,   # subagent defs
               parent_version?, created_by, created_at, note, status: draft|active|retired}
Skill         {namespace, name, description, owners[]}
SkillVersion  {version, sha256 (bundle), size, manifest: {name, description,
               allowed_tools[], files[], entry?}, compatibility: {agent: "claude-code>=…"},
               signature?, created_by, created_at, note, status}
Label         {target: prompt|skill, namespace, name, label, environment, version,
               moved_by, moved_at, reason}                    # one row per (label, env)
Environment   dev | staging | production | <custom>            # per namespace
EvalLink      {target, version, eval_system, run_url, dataset, metric, value,
               verdict: pass|fail|inconclusive, recorded_at}
Lock          {lock_id (sha256 of contents), created_at, environment, requester,
               entries: [{kind, namespace, name, ref, version, sha256}]}
```

- **Versions are immutable.** A fix is a new version; `parent_version` keeps the line.
- **References** are `namespace/name@label` or `namespace/name@<version>` or
  `namespace/name@sha256:<…>`. A label resolves per environment.
- **Promotion rule** (configurable per namespace): moving `production` requires the
  version to have at least one `pass` EvalLink and an approver other than its author.

## API (OpenAPI 3.1 sketch)

```yaml
openapi: 3.1.0
info: {title: Prompt and Skill Manager, version: 1.0.0}
paths:
  /v1/prompts:                                   {get: list, post: create prompt}
  /v1/prompts/{ns}/{name}:                       {get: prompt + labels}
  /v1/prompts/{ns}/{name}/versions:              {get: list, post: create version}
  /v1/prompts/{ns}/{name}/versions/{v}:          {get: version (template, variables, hints)}
  /v1/prompts/{ns}/{name}/versions/{v}/render:   {post: {vars} → text}       # validates vars
  /v1/prompts/{ns}/{name}/labels/{label}:        {put: {environment, version, reason}}
  /v1/skills:                                    {get: list, post: create skill}
  /v1/skills/{ns}/{name}/versions:               {get: list, post: upload bundle (multipart)}
  /v1/skills/{ns}/{name}/versions/{v}:           {get: manifest}
  /v1/skills/{ns}/{name}/versions/{v}/bundle:    {get: tar.gz, header Digest: sha-256=…}
  /v1/skills/{ns}/{name}/labels/{label}:         {put: {environment, version, reason}}
  /v1/evals:                                     {post: attach EvalLink}
  /v1/resolve:                                   {post: {environment, refs[]} → resolved entries}
  /v1/locks:                                     {post: {environment, refs[]} → Lock}
  /v1/locks/{lock_id}:                           {get: Lock}
components:
  securitySchemes:
    oidc: {type: openIdConnect}                  # people
    engine: {type: http, scheme: bearer}         # engine tokens / workload identity
```

**Events** (CloudEvents 1.0, the services' own namespace, so they are useful without
mission control): `dev.prompts.version.created`, `dev.prompts.label.moved`,
`dev.prompts.eval.attached`, `dev.skills.version.published`, `dev.skills.label.moved`,
`dev.prompts.lock.pinned` (published by the engine when a lap starts, with `run_id`).

## How an engine fetches and pins (per lap)

1. The lap's **kit file** (in the factory repo per product, e.g.
   `products/<product>.kit.yaml`) lists refs: `factory/compliance-review@production`,
   `factory/skills/pr-diff-reader@production`, ….
2. At lap start the engine calls `POST /v1/locks` once. The response is immutable and
   carries every version and sha256.
3. The engine downloads content by sha256 (cache keyed by hash, so a second lap is
   free), verifies each digest, and publishes `dev.prompts.lock.pinned` with the run id.
4. A **rerun** of the same run reuses the stored lock id, never re-resolves labels.
   A **new lap** resolves again, so a label move takes effect at the next lap boundary,
   never mid-run.
5. If the service is unreachable, the engine uses the last lock it has for that kit
   and environment and says so in the event (`stale_lock: true`); with no lock at all
   the lap does not start.

## How skills and prompts are materialized into a lap

The lap's checkout is the product repo, which changes only through PRs, so nothing is
written into tracked paths:

- **Skills** go into a **lap-scoped Claude Code config directory**
  (`CLAUDE_CONFIG_DIR=<lap state>/claude`, `skills/<name>/`), not into the repo's
  `.claude/`. The lap's agents see them as user-level skills; the repo stays clean, and
  nothing can be committed by accident.
- **Subagent definitions** (prompt kind `subagent`) are written the same way, to
  `<lap state>/claude/agents/<name>.md`, with their frontmatter.
- **Node prompts of project-level workflows** (e.g. the compliance agent's) are
  rendered into the workflow's command files in the lap's private copy of that
  workflow, outside the product repo.
- **The pinned Archon workflows' own prompts are not touched.**
- The materializer is the same engine-side component as the memory materializer
  (Stage I5), with one job each: memory documents, prompts, skills. Holdouts are never
  part of a kit.

Open: whether Claude Code reads both a lap-scoped `CLAUDE_CONFIG_DIR` and the repo's
`.claude/` together in the pinned version used by Archon (the repo's project settings
include the holdout fence hook, which must keep working). Checked in step T6.

## Ports, so either service can be replaced

| Port | Operations | Adapters |
|---|---|---|
| **PromptRegistry** | list, get, create_version, move_label, resolve, attach_eval | built-in service; **git repo** (files per prompt, tags as versions, a labels file); Langfuse prompt management; MLflow prompt registry |
| **SkillRegistry** | list, get_manifest, publish_version, fetch_bundle, move_label, resolve | built-in service; **git repo**; **OCI artifacts** in any registry (skills as OCI artifacts, digests as versions, tags as labels) |
| **BlobStore** | put/get by hash | Cloud Storage, S3, local disk |
| **MetaStore** | the tables above | Firestore, Postgres |

Each port has a contract test suite, run against every adapter, like mission control's.
Engines depend only on `resolve`, `locks` and fetch-by-hash, so even a git-repo adapter
with no server is enough to run.

## Security

- **People** sign in with OIDC. Roles per namespace: `viewer`, `author` (create
  versions), `approver` (move `production`), `admin`. An author cannot approve their own
  version for `production`.
- **Engines** use an engine token or workload identity with `resolve`/`locks`/fetch
  only: they can never create versions or move labels.
- **Skills are code.** Their scripts run inside a lap, so a skill version needs a
  review like code: manifest `allowed_tools` is shown on the review, bundles are
  checked for secrets and binaries, digests are verified on download, and optional
  signing (cosign or Sigstore) can be required per namespace.
- **No secrets** in templates or bundles (scanner on create); variables that hold
  secrets are injected by the engine from its own Secrets port at render time, never
  stored.
- **Audit**: every create and label move is an event and a row with who and why.
- **Holdouts**: the holdout agents' prompts may be managed here, but holdout scenario
  text never is.

## Mission control integration (client only)

- Console: each run shows its lock (prompt and skill versions) from
  `dev.prompts.lock.pinned`; a version links to the manager's diff view.
- Problems: a rule opens a problem when a lap ran with `stale_lock: true`.
- The compliance agent is the first consumer: its review prompt and rules move from
  `agents/compliance/` files into the manager once T5 is done.

## Open questions

- Hosting: its own Cloud Run service and Firestore database, or an existing product via
  an adapter from day one (for example Langfuse self-hosted)? The ports allow either.
- Whether the git-repo adapter alone is enough for the first year (no server at all),
  given that labels then move by PR.
- The `CLAUDE_CONFIG_DIR` plus repo `.claude/` behaviour above.
