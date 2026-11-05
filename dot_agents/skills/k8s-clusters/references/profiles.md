# Cluster profiles: schema 1

One TOML file per host says which clusters agents may use and how to reach them. It holds internal names, so it is never committed. It lives at `~/.config/k8s-clusters/profiles.toml`, or at the path in `K8S_CLUSTERS_PROFILES`. Write it from section 3 of the Linear runbook "Runbook: build and run on our Dynamo k8s clusters", and keep it current with the runbook.

`k8s-check`, `k8s-guard` and the `k8s-leases` bb plugin all read this file. `k8s-check --list` validates it and prints it as JSON, for a reader without a TOML parser.

`profiles.example.toml` next to this file is a complete example with made-up names.

## Top level

| Key | Type | Meaning |
|---|---|---|
| `schema_version` | integer | Must be `1`. A reader refuses any other value. |
| `[bot]` | table | The agents' own identity. Required. |
| `[clusters.<name>]` | table | One per cluster. `<name>` is the cluster's Teleport name, the name tools take on the command line. |

## `[bot]`

| Key | Type | Meaning |
|---|---|---|
| `user` | string | The username `kubectl auth whoami` must print for the bot. Any other identity is refused. |
| `kubeconfig` | string | The bot's kubeconfig, written by `tbot`. `~` is expanded. `KUBECONFIG` must name exactly this file. After symlinks are resolved, it must lie under `~/.local/share/tbot/`, where the tbot config writes it. A path with a `.kube` or `.tsh` directory in it, as written or resolved, is refused: those hold the owner's own login. |
| `developer` | string | The value of the `dynamo.nvidia.com/developer` label on our namespaces. |
| `namespace_prefix` | string | The prefix of every namespace we may write in, ending in `-`. |

## `[clusters.<name>]`

Every profile needs `context`, `allowed` and `source`. A profile with `allowed = "never"` needs nothing else: it exists so tools refuse the cluster by name. Every other profile needs every key below except `gpu_taint` and `notes`.

| Key | Type | Meaning |
|---|---|---|
| `context` | string | The kubeconfig context name. Tools pass it as `--context` on every call. |
| `allowed` | string | `first-choice`, `qa-idle` (only when QA and CI are idle; check first), or `never` (tools refuse it). Clusters the runbook says need the owner's word are `never`; clusters not on its list are left out, and tools refuse them as unknown. |
| `source` | list of strings | Where each value came from: the ops inventory page and its date, a path in the ops config repository, a `k8s-check` run. |
| `bot_can_write` | boolean | Whether the bot may write in our namespaces on this cluster. `false` makes the cluster read only for every tool. |
| `arch` | string | `amd64` or `arm64`. Images must match. |
| `gpu_product` | string | The value of the `nvidia.com/gpu.product` node label. |
| `gpu_type` | string | The GPU type key the `k8s-leases` plugin counts against, in lower case: `gb300`, `b200`, `gb200`, `h100` today. Other keys are allowed (`h200`, `a100`); the plugin refuses a key it has no limit for. |
| `gpus_per_node` | integer | GPUs on one node. |
| `gpu_taint` | table | The taint on GPU nodes that a GPU pod must tolerate: `key`, optional `value`, `effect` (`NoSchedule`, `PreferNoSchedule` or `NoExecute`). Leave `value` out for a taint tolerated with `operator: Exists`. Leave the table out when GPU nodes carry no such taint. Never tolerate other taints: they mark reservations. |
| `storage_class` | string | The storage class for a PVC. |
| `model_cache` | string | The name of the shared model cache PVC in each namespace, or `""` when the cluster has none. |
| `pull_secret` | string | The image pull secret the cluster copies into each namespace, or `""` when none. |
| `reaper` | boolean | Whether the GPU reaper runs on this cluster. `k8s-check` warns when this disagrees with the pods running in the `gpu-reaper` namespace. |
| `operator_namespace` | string | The namespace of the Dynamo operator, usually `dynamo-system`. |
| `notes` | string | Optional free text. |

Unknown keys are errors, so a typo does not silently drop a rule.

## `k8s-check` and the CRD version

No profile field names a DGD API version: it depends on the DGD, not the cluster. `k8s-check --want-crd <version>` fails when the cluster's DGD CRD does not serve that version. Without the flag it only reports the served versions.
