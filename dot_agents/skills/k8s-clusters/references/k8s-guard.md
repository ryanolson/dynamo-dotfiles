# k8s-guard

`k8s-guard` is the only way agents write to a shared cluster. It wraps `kubectl`, runs as the agents' bot, and refuses any write outside our namespaces, our kinds, or our GPU lease. The cluster runbook in Linear says when to use each command. This file is the reference for what the guard checks.

Every command prints one JSON object on stdout. A refusal exits non-zero with `{"error": "...", "code": "..."}`. The code is stable; the message is for people.

## Commands

| Command | Who may run it | What it does |
|---|---|---|
| `ns create <purpose> --issue <n> --hours <h>` | a bb thread | Creates `<namespace_prefix><n>-<purpose>-<suffix>`, where the suffix is 5 random letters and digits from the CSPRNG, with the opt-in labels, `owner`, `wcep.agent/thread`, `wcep.agent/issue`, and the annotation `wcep.agent/expires-at`. `h` is at most 4. A name that exists is refused, and a name longer than 63 characters. It records the name and UID in the thread's ledger. |
| `-n <ns> lease acquire --gpus <n> --hours <h>` | the namespace's thread | Calls `bb k8s lease acquire` with the profile name, the namespace UID, the thread, and the profile's GPU type. Writes the lease record (below). Prints the pod label and pod affinity that GPU pods must carry. |
| `-n <ns> secret hf\|ngc` | the namespace's thread | Pipes `fleet secret get` into `kubectl create secret ... --from-file=<key>=/dev/stdin`. Prints only the secret's name. |
| `-n <ns> apply -f <file>` | the namespace's thread | Applies DGDs, ConfigMaps and ClusterIP Services, after the checks below. |
| `-n <ns> delete <kind>/<name>` | the namespace's thread | Deletes one DGD, ConfigMap, Service, Secret, PVC or PodCliqueSet. The lease record, the model cache PVC and the pull secret are refused. Like `apply`, it takes the lease's slot, or needs a namespace that holds no GPUs. |
| `down <ns> [--uid <uid>] [--lease <id>]` | the namespace's thread, or anyone with teardown authority | Releases an active lease first (owner only). Deletes each DGD with its UID as a precondition, waits for GPU pods to go, then deletes the namespace with its UID as a precondition. |
| `expire <ns> [--uid <uid>] [--lease <id>]` | anyone with teardown authority | Sets every replica count of every DGD to 0. |
| `extend <ns> [--uid <uid>] [--lease <id>]` | anyone | Copies the lease's `expires_at`, unchanged, into the lease record. It only replaces the record `lease acquire` wrote, under the `resourceVersion` it read, and never creates one. The lease must be `active` and match the cluster, the namespace and the UID. It never moves the stored expiry earlier, so a late call cannot undo a newer extension. |
| `-n <ns> selftest fences` | the namespace's thread | For the first run on a cluster. On the first DGD the guard applied, it sends each teardown fence once with a wrong UID or a stale `resourceVersion`: two JSON patches, a DGD delete, a namespace delete, and a lease record replace. Each payload changes nothing even if the server ignored its fence. It prints each server response and reads the objects again. It fails with `fence_failed` unless every request was refused for its fence's own reason (`testing value`, `Precondition failed`, `the object has been modified`) and nothing changed. Another error, such as Forbidden, proves nothing. |
| `inspect <ns>` | anyone | Read only. Prints `uid`, `thread`, `expires_at`, `dgd_gpus`, `pod_gpus`, `gpu_type`, `gpu_pods_left`, and also `allocation` and `lease_id`. A missing namespace gives code `not_found`; any other failure gives another code. |

Every command needs `--context`. `-n` can come anywhere.

The commands for the namespace's thread (`lease acquire`, `secret`, `apply`, `delete`, `selftest fences`, and the owner's `down`) take the namespace from the thread's ledger, `$BB_THREAD_STORAGE/k8s-ledger.jsonl`. A given `-n` must be a namespace this thread created, and its UID must still be the one recorded. Without `-n`, the thread's only live namespace is used. Everything else is refused, including `scale`, `patch`, `edit`, `replace`, `set`, `label`, `annotate`, `get`, and `-A`.

## Identity and cluster

- `KUBECONFIG` must name exactly the bot's kubeconfig from the profiles, which the parser allows only under tbot's directory in the account's home. The guard never runs `tsh`.
- It reads that file once into a memory file sealed against writes (`k8s_profiles.sealed_copy`), and passes it to every `kubectl` call as `/proc/self/fd/<n>`. Replacing or rewriting the file later changes nothing. `kubectl` gets no `KUBECONFIG`, no `KUBECTL_*` variable, and `KUBECTL_KUBERC=false`.
- Before any cluster call, `kubectl config view` (local) must show that the context's user holds only a client certificate or exactly the exec plugin tbot writes: an installed tbot (`/usr/local/bin/tbot` or `~/.local/bin/tbot`) with `kube credentials --destination-dir=<the kubeconfig's directory>`. A token, an impersonation, or another exec command such as `tsh` is refused with `kubeconfig_credentials`.
- `--context` must equal the `context` of one profile. A profile with `allowed = "never"` is refused. A profile with `bot_can_write = false` allows only `inspect`.
- The guard calls the lease plugin with the profile name (the `[clusters.<name>]` key), and checks that a lease's `cluster` equals it.

## The lease record

The bot's rights come from RoleBindings inside the namespace, so the guard writes the Namespace object only once, when it creates it. Lease state that changes lives in a ConfigMap, `wcep-agent-lease`, in the namespace: `lease_id`, `expires_at`, `thread`, and `uid`. `lease acquire` creates it, and `extend` replaces it with the `resourceVersion` it read, so a concurrent change fails. `apply` and `delete` refuse it.

The expiry of a namespace is the record's `expires_at` when the record's `uid` matches the namespace. Otherwise it is the creation annotation. The record only points to the lease. The plugin's `bb k8s lease show` decides.

## Apply

The rules in this section and the next are pure functions in `scripts/k8s_guard_rules.py`.

The guard parses no YAML. `kubectl label --local` (no API call) reads the file and labels every object `wcep.agent/namespace-uid=<namespace UID>`. The guard sends that JSON to `kubectl apply --dry-run=server -o json`, then the same JSON to the real apply. Every check runs on the JSON from the dry run, so defaults and webhook changes are checked too.

- **Kinds:** `DynamoGraphDeployment` (`nvidia.com/v1alpha1` or `v1beta1`), `ConfigMap`, and `Service` of type `ClusterIP`. Each object must be in the `-n` namespace.
- **DGD fields:** only the fields the guard can count. Refused: scaling adapters, enabled autoscaling, planner, EPP and LPX components, `roles`, DRA resource claims, any GPU resource other than `nvidia.com/gpu`, PVCs the DGD would create, and any field the guard does not know.
- **Pod specs:** no `hostPath`, `priorityClassName`, `nodeName`, `schedulerName`, privileged containers, `hostNetwork`, `hostPID` or `hostIPC`, and no toleration with `operator: Exists` and no key. Every container needs `cpu` and `memory` in both requests and limits.
- **Name:** a DGD name has at most 39 characters. An object may appear only once in a file.
- **GPU type:** every GPU pod needs `nodeSelector` `nvidia.com/gpu.product` equal to the profile's `gpu_product`, the GPU the lease counts.

## Budget

A DGD's allocation is the larger of two numbers: its spec (replicas times nodes times GPUs per pod) and the GPUs its pods still request. Terminating pods count. Finished pods do not. GPU pods that belong to no DGD count on their own. The namespace's allocation is the sum.

`kubectl` writes the objects of a file one at a time and goes on after a failure, so any subset of the file may land. So each DGD in the file counts at the larger of its old and new spec: a shrink that fails never pays for a growth. Shrink in one apply, then grow in the next.

With no lease record, that allocation must be 0. With a record, the guard:

1. reads the lease. It must be `active` and match this thread, the profile name, the namespace, its UID, and the profile's GPU type;
2. takes the lease's slot (`bb k8s lease op begin`);
3. runs the dry run and every check, and reads the DGDs and pods again;
4. refuses when the allocation is more than the lease;
5. without `multi_node`, refuses when the allocation is more than one node of the profile, and when a GPU pod lacks the label `wcep.agent/lease=<id>` or a required pod affinity to that label on `kubernetes.io/hostname`;
6. reads the namespace UID once more, then applies the objects one request at a time. Before each one it refuses when less than 40 seconds of the slot are left, and it stops `kubectl` before the slot ends. Objects already applied stay; the budget above counts every subset;
7. calls `op end`. When it reports the lease as no longer `active`, the guard runs `expire` at once and exits with `lease_revoked`.

Every write in a namespace with a lease record takes the slot, GPU or not. `secret` and `delete` follow the same rule.

## Teardown authority

`down` by the owner releases an active lease first. Any other `down`, and every `expire`, needs one of these:

- the plugin says the lease is `revoking` or `releasing`, for this cluster, namespace and UID;
- the plugin cannot be reached, `--uid` matches the namespace, and the namespace's expiry has passed.

A JSON error from the plugin, such as an unknown lease, is an answer, not an outage, and is refused. So is a lease the plugin reports as `released` or `torn-down`. `down` stops with `pods_left` while GPU pods remain, and keeps the namespace.

## A re-created namespace

A namespace name is never made twice: `ns create` adds a random suffix and refuses a name that exists. So no namespace can be re-created under a name an earlier one had, through the guard. The checks below stay as defense in depth.

`k8s-leases` sends `expire`, `down` and `extend` with the UID it stored, without reading the namespace first. The guard refuses a `--uid` that differs from the namespace, before any write.

A read cannot fence a later write, so the writes carry their own fences, which the API server checks:

- `expire` and `down` select only DGDs labelled with the namespace's UID. A namespace re-created under the same name holds none.
- Each `expire` patch tests the DGD's UID and that label. Each DGD delete and the namespace delete carry the UID as a precondition.
- `extend` replaces the lease record under its `resourceVersion` and never creates one.

The guard also reads the UID again before each write, so most races stop before a request is sent.

## Ledger

Every write appends one JSON line to `${XDG_STATE_HOME:-~/.local/state}/k8s-guard/ledger.jsonl` on the host. When `BB_THREAD_STORAGE` is set, it also appends the line to `$BB_THREAD_STORAGE/k8s-ledger.jsonl`. A line holds the time, the calling thread, the cluster, the context, the namespace, its UID, the object, the GPUs, the direction (`up`, `down` or `extend`), the lease, and the issue. No secret value reaches stdout, stderr, or a ledger.

## Not proven without a cluster

The tests use fakes. The first run on a cluster must confirm these, and run `selftest fences` in its smoke namespace:

- the bot can create a namespace with labels and annotations, and can delete it;
- the API server accepts the `DeleteOptions` body that `kubectl delete --raw <path> -f -` sends without a `Content-Type`;
- the dry run of a stock DGD adds no field the guard refuses.

## Tests

```bash
timeout 300 python3 test/test-k8s-guard.py </dev/null
```
