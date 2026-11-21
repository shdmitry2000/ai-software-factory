# deploy/

Desired state for the factory's agents and system parts, read by
`mission_control.deployspec` and applied through the AgentRuntime port.

| File | Holds |
|---|---|
| `components/<name>.yaml` | one deployable thing: image, build, command, LLM method, env, events it consumes and emits |
| `hosts.yaml` | where things can run (`local` = the main node) |
| `deployments.yaml` | which component runs on which host, with replicas and product |

Schema: `mission-control/schema/deploy.v1.json`. Check the files:

    PYTHONPATH=mission-control uv run --project mission-control python -m mission_control.runtime spec

Never put a secret here: env names that look like one are refused. The LLM credential
reaches a container as a mounted file (see docs/deploy-and-hosts-plan.md §3).
