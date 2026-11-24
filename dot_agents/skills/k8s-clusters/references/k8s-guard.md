# k8s-guard

`k8s-guard` wraps `kubectl` and runs as the agents' bot. It checks that each write targets an allowed namespace and resource kind. GPU workloads need a lease or a spend record. The cluster runbook in Linear says when to use each command.

Every command prints one JSON object on stdout. A refusal exits non-zero with `{"error": "...", "code": "..."}`. The code is stable; the message is for people.

## Commands

| Command | Who may run it | What it does |
|---|---|---|
| `ns create <purpose> --issue <n> --hours <h>` | a bb thread | Creates `<namespace_prefix><n>-<purpose>-<suffix>`, where the suffix is 10 random letters and digits from the CSPRNG, with the opt-in labels, `owner`, `wcep.agent/thread`, `wcep.agent/issue`, and the annotation `wcep.agent/expires-at`. `h` is at most 4. A name that exists is refused, and a name longer than 63 characters. It records the name and UID in the thread's ledger. Where the bot may not create a namespace (Forbidden at the cluster scope, as on our clusters), it refuses `namespace_create_denied` and names `ns claim`. |
| `-n <premade> ns claim --gpus <n> --hours <h> [--audit-trial]` | a bb thread | Claims the profiles' pre-made namespace (`[bot] premade_namespace`) with a `k8s-leases` lease, then opens the fence's gate for it. Refuses `namespace_not_enrolled` without the fence label. `--audit-trial` (with `--gpus 1`) asks for a trial claim. See "A claimed namespace" and "The fence policy" below. |
| `-n <ns> lease acquire --gpus <n> --hours <h>` | the namespace's thread | Calls `bb k8s lease acquire` with the profile name, the namespace UID, the thread, and the profile's GPU type. Writes the lease record (below). Prints the pod label and pod affinity that GPU pods must carry. |
| `-n <ns> secret hf\|ngc\|ngc-push` | the namespace's thread | Creates the Secret from `fleet secret get`, on stdin only, with `kubectl create -f -`: create-only, labelled with the namespace UID and the lease. `ngc` is the pull-only key (`op://Fleet/NGC Pull Key/credential`); `ngc-push` is the push credential and is never used for a pull secret. `ngc-push` also needs `--exposure-accepted`, and refuses `push_not_approved` without it: the value is readable by everyone in the access group once it is in the namespace. The flag is a stated step, not an authority check. Prints only the secret's name. |
| `-n <ns> secret git --repo <owner>/<repo>` | the namespace's thread | Creates the read-only deploy key as Secret `git` (key `id_ed25519`, stored with one trailing newline) and the pinned GitHub host key as ConfigMap `git-known-hosts`, both from the guard, both lease-labelled. The ref is the convention `op://Fleet/<owner>-<repo> deploy key/private key`; the item's `private key` field must be the OpenSSH key form. The key is never printed. It refuses `secret_unavailable` when the item is absent; there is no fallback. |
| `-n <ns> build run --repo <url> --commit <40 hex> --dockerfile <path> --image <name> [--arch amd64\|arm64]` | the namespace's thread, under its live lease (the claim in the pre-made namespace) | Renders one image-build Job from the fixed template (see "Build Jobs"), takes the lease's slot, records one ledger line and prints `{"job": "<name>"}` only. Refuses a commit that is not 40 hex, an image name it cannot tag, an arch that is not the profile's, a forbidden field, a Job without the lease label on its pod template, and a name already taken (`job_exists`). |
| `-n <ns> build show <job>` | anyone | Read only. Prints the Job's state and the last 200 log lines, with credential-shaped strings redacted. A failed Job exits non-zero with `build_failed`, `tag_exists`, `registry_denied` or `registry_error`. |
| `-n <ns> build index --commit <40 hex> --image <name>` | the namespace's thread, under its live lease | The manifest-list Job: `crane index append -t ...:<12 hex> -m ...:<12 hex>-amd64 -m ...:<12 hex>-arm64`, then `crane manifest` and `crane digest`. Runs under the still-live amd64 claim. |
| `-n <ns> build delete <job>` | the namespace's thread, under its live lease | Deletes the Job with `propagationPolicy: Background`, so its Pods go with it. |
| `-n <premade> dev run --image <name> --image-commit <40 hex> --digest sha256:<64 hex> --repo <url> --commit <40 hex> [--arch <arch>]` | the live claim's thread | Starts one dev Job under the lease fence. Prints `{"job": "<name>"}`. See "Dev Jobs". |
| `-n <premade> dev show <job>` | anyone | Reads the dev Job's state, pinned image digest and seconds left on its deadline. Reports the Pod's phase, node and restart count when a Pod exists. |
| `-n <ns> apply -f <file>` | the namespace's thread, or the thread named in the GPU spend record | Applies DGDs, ConfigMaps and ClusterIP Services. In the pre-made namespace without fence enrollment, it can also create one [GPU Job with a spend record](#gpu-spend-without-a-lease-fence). |
| `-n <ns> delete <kind>/<name>` | the namespace's thread | Deletes one DGD, Job, ConfigMap, Service, Secret, PVC or PodCliqueSet, with its UID and `resourceVersion` as preconditions. A Job's delete carries `propagationPolicy: Background`. An object labelled with another lease is refused (`object_not_yours`); in a claimed namespace, so is one without this lease's label. The lease record, the model cache PVC and the pull secret are refused. Like `apply`, it takes the lease's slot, or needs a namespace that holds no GPUs. |
| `down <ns> [--uid <uid>] [--lease <id>] [--fence-retired <url>]` | the namespace's thread, or anyone with teardown authority | Releases an active lease first (owner only). Deletes each DGD labelled with the lease being torn down: it reads the DGD again, checks that its UID is still the one the list gave and that its lease label still holds, then deletes it under that listed UID and the `resourceVersion` just read. A DGD that changed in between is left (`dgds_kept`), not deleted. Waits, at most `K8S_GUARD_DOWN_WAIT_SECONDS` (300), for every GPU pod in the namespace to go. Then deletes the namespace with its UID as a precondition, whatever is left, and reports `gpu_pods_left`. A namespace already being deleted gets no second delete (`terminating: true`). With no lease id (no `--lease` and no record) it deletes no DGD and says so in `dgds_skipped`. In a claimed namespace it never deletes the namespace: see "A claimed namespace". `--fence-retired` is exit 2 of "The fence policy". |
| `expire <ns> [--uid <uid>] [--lease <id>]` | anyone with teardown authority | Sets every replica count of every DGD labelled with the lease being torn down to 0. With no lease id (no `--lease` and no record) it scales nothing and says so in `dgds_skipped`. |
| `extend <ns> [--uid <uid>] [--lease <id>]` | anyone | Copies the lease's `expires_at`, unchanged, into the lease record. It only replaces the record `lease acquire` wrote, under the `resourceVersion` it read, and never creates one. The lease must be `active` and match the cluster, the namespace and the UID. It never moves the stored expiry earlier, so a late call cannot undo a newer extension. |
| `-n <ns> selftest fences` | the namespace's thread | For the first run on a cluster. On the first DGD the guard applied, it sends each teardown fence once with a wrong UID, a stale `resourceVersion` or a wrong lease label: three JSON patches, two DGD deletes, a namespace delete (not in a claimed namespace, which gets no Namespace request at all), and a lease record replace. Each payload changes nothing even if the server ignored its fence. It prints each server response and reads the objects again. It fails with `fence_failed` unless every request was refused for its fence's own reason (`testing value`, `Precondition failed`, `the object has been modified`) and nothing changed. Another error, such as Forbidden, proves nothing. |
| `fence probe [<premade>]` | anyone | Read only: two server dry-run Pods. Prints `canary`, `mode`, `scope`, `armed`, the matched texts, `lease_fence`, and the `cause` when not armed. See "The fence policy". |
| `fence render --owner <o> --contact <c> --runbook <r> [--bot-user <u>]` | anyone | Local, no cluster call: the two fence policies for ops, filled from the profiles. |
| `fence record [<premade>] [--since <time>]` | anyone | Read only: appends the audit window's new rows from PolicyReports and Kyverno Events, and counts the probes this host sent (since `--since`). See "The recorder". |
| `-n <premade> fence trial close` / `fence trial delete-pod <pod>` | the trial claim's thread | A trial claim's two trial-only steps; refused outside a trial (`trial_only`). |
| `inspect <ns>` | anyone | Read only. Prints `uid`, `thread`, `expires_at`, `dgd_gpus`, `pod_gpus`, `gpu_type`, `gpu_pods_left`, and also `allocation`, `lease_id`, `developer` and `agent_rbac` (the Namespace's two opt-in labels), and `lease_fence` (the fence label). A missing namespace gives code `not_found`; any other failure gives another code. |

Every command needs `--context`. `-n` can come anywhere.

The commands for the namespace's thread (`lease acquire`, `secret`, `apply`, `delete`, `selftest fences`, and the owner's `down`) take the namespace from the thread's ledger, `$BB_THREAD_STORAGE/k8s-ledger.jsonl`. A given `-n` must be a namespace this thread created, and its UID must still be the one recorded. Without `-n`, the thread's only live namespace is used. The pre-made namespace is different: see "A claimed namespace". Everything else is refused, including `scale`, `patch`, `edit`, `replace`, `set`, `label`, `annotate`, `get`, and `-A`.

## GPU spend without a lease fence

For an approved GPU run in the profile's pre-made namespace, put the spend record in the Job manifest under `metadata.annotations`. The record stays on the Job.

```yaml
metadata:
  annotations:
    wcep.agent/gpu-spend: >-
      {"thread":"thr_example","gpus":4,"start":"2026-11-24T16:00:00Z","max_hours":0.5,
      "cluster":"lab-x86-qa","namespace":"dyntest-alice-agent"}
spec:
  activeDeadlineSeconds: 1200
  template:
    spec:
      nodeSelector:
        nvidia.com/gpu.product: NVIDIA-A100-SXM4-80GB
      tolerations:
        - key: nvidia.com/gpu
          operator: Equal
          value: "true"
          effect: NoSchedule
```

Use the submitting BB thread ID, the cluster profile name, and the target namespace. Set `gpus` to the number of requested devices per Pod times Job parallelism, with a minimum of one Pod for a suspended Job. Select full A100s or MIG slices with `nvidia.com/gpu.product` in the Pod template's `nodeSelector`. Set the RFC 3339 start time with a time zone. Set `max_hours` above zero and at most four hours. Set the Job's deadline to a positive whole number of seconds that fits in the time left in its window.

Pass one `batch/v1` Job to `k8s-guard --context <ctx> -n <premade> apply -f <file>` during the recorded window. The guard uses this path when the namespace lacks `dynamo.nvidia.com/agent-lease-fence: "true"`. It checks the record and deadline before the first create request and checks the server dry run. Pick a new name for each run. The sweep checks the record, deadline, window and total live Pod GPUs, including for workloads submitted through the RBAC path.

## Identity and cluster

- `KUBECONFIG` must name exactly the bot's kubeconfig from the profiles, which the parser allows only under tbot's directory in the account's home. The guard never runs `tsh`.
- Each run calls `k8s_profiles.prepare_bot_kubeconfig`. It finds the installed `kubectl` ELF binary whose sha256 is the profile's `[bot] kubectl_sha256`, never one found on `PATH`. It renders a kubeconfig for the one context from an allowlist of tbot's fields, with tbot's exec plugin replaced by a credential snapshot taken now (the certificate lives about an hour). The result sits in a memory file sealed against writes.
- Every `kubectl` call uses that `kubectl`, the rendered file as `/proc/self/fd/<n>`, no `KUBECONFIG`, no `KUBECTL_*` variable, and `KUBECTL_KUBERC=false`.
- The renderer's refusals keep their codes: `kubectl_not_found`, `kubectl_mismatch`, `kubeconfig_not_bot`, `kubectl_error`, `context_missing`, `kubeconfig_credentials`, `credentials_expired`. The rules are in `references/profiles.md` and `SKILL.md` ("Threat model").
- `--context` must equal the `context` of one profile. A profile with `allowed = "never"` is refused. A profile with `bot_can_write = false` allows only `inspect`.
- The guard calls the lease plugin with the profile name (the `[clusters.<name>]` key), and checks that a lease's `cluster` equals it.

## The lease record

The bot's rights come from RoleBindings inside the namespace, so the guard writes the Namespace object only once, when it creates it. Lease state that changes lives in a ConfigMap, `wcep-agent-lease`, in the namespace: `lease_id`, `expires_at`, `thread`, and `uid`. `lease acquire` creates it, and `extend` replaces it with the `resourceVersion` it read, so a concurrent change fails. `apply` and `delete` refuse it.

The expiry of a namespace is the record's `expires_at` when the record's `uid` matches the namespace. Otherwise it is the creation annotation. The record only points to the lease. The plugin's `bb k8s lease show` decides.

## A claimed namespace

On our clusters the bot cannot create a namespace. Ops made one per cluster for it, named in the profiles as `[bot] premade_namespace`, with the labels `dynamo.nvidia.com/developer` and `dynamo.nvidia.com/developer-agent-rbac: "true"`. The bot cannot label, annotate or delete it, so nothing in the namespace can mark which task owns it. The `k8s-leases` plugin holds that instead: the lease's row in its database is the claim, the GPU charge and the completion.

- `ns claim` reads the namespace and checks both labels. It keeps a request id in the thread ledger before it calls `bb k8s lease acquire --claim`, so a retry after a lost reply asks again with the same id and gets the same answer. Only `--gpus` and `--hours` are terms a retry must repeat: a namespace re-created since does not block it, and the claim is recorded against the UID the request named, so every write in it refuses `uid_mismatch` and the new namespace stays for on-call. A refusal of that retry is the answer the pending request was missing: the guard records it, then makes one fresh request. It names the namespace's latest lease (`bb k8s lease latest`) as the one this claim replaces. The plugin takes the owner from the calling thread, checks the exact profile name, both labels and the live UID on a host, and decides in one transaction. The same owner can adopt its own matching active lease after losing its ledger. Nobody else can, and a lease being torn down keeps the namespace.
- Every write under the fence in the pre-made namespace needs this thread's claim. The lease must be `active` and match the thread, cluster, namespace, UID and GPU type. The ledger line points to the lease. For approved unfenced GPU Jobs, see [GPU spend without a lease fence](#gpu-spend-without-a-lease-fence).
- Teardown takes only the plugin's word (`revoking` or `releasing`). The namespace keeps no expiry, so there is no offline teardown; with the plugin unreachable, `down` and `expire` refuse `lease_unavailable`.
- The owner's `down` releases the lease and returns. The plugin then runs `down` on a host, which:
  0. probes the fence, closes its gate for this lease and `seq`, and reads it back ("The fence policy");
  1. resolves the lease's open write attempts from storage (below);
  2. scales the lease's DGDs to 0, then deletes each object labelled `wcep.agent/lease=<id>` of the kinds DGD, Job, PodCliqueSet, Service, ConfigMap, Secret and PVC (not the model cache PVC or the pull secret), each with its UID and `resourceVersion` as preconditions. A Job's delete carries `propagationPolicy: Background`, because a `batch/v1` Job delete otherwise orphans its Pods, which carry the lease label and would hold teardown open for the Job's 24-hour retention;
  3. reads the lease's labelled objects again, Jobs included, adding pods, PodCliques, Deployments and StatefulSets. Terminating pods count;
  4. waits the drain, probing the fence, then reads the inventory and probes once more;
  5. reports `complete` only when every attempt is resolved, nothing changed hands, every list was read, nothing of the lease is left, the gate reads `closing` for it, and the fence was armed throughout. Otherwise it refuses `teardown_incomplete`, with what it resolved and what is left.
- `down` never deletes, labels or patches the pre-made namespace. The plugin frees the lease's GPUs only through one transaction conditional on that completion (`k8s-leases` README). Running `down` again after a crash goes on from where it stopped.
- A pod with the DGD's name label but not the lease label is never selected, waited for or deleted.

Claims stay off until the fence enforces: before every claim `k8s-leases` asks a host for `fence probe`, and refuses `enforcement_unhealthy`, with the probe's cause, unless it reads `enforcing` with scope `finegrained` (or `audit` for a trial). The gate is a code option of the plugin, never a setting.

Install order: this guard calls `bb k8s lease attempt begin` on every leased write, in a namespace it created too, and `bb k8s lease latest` for a claim. A `k8s-leases` older than the claim protocol answers those with usage text, which the guard reads as an unreachable plugin, so every leased write would refuse `lease_unavailable`. Install the plugin first, then let hosts take this guard (`chezmoi update` or `chezmoi-headless-update`). The older guard works with the newer plugin.

## The fence policy

A claim of the pre-made namespace is fenced by two Kyverno ClusterPolicies that ops install: `agent-lease-fence` and `agent-lease-fence-canary`. The template is `policy/agent-lease-fence.template.yaml`; `fence render` fills it from the profiles. The policies see the request and one live read of the gate. They cannot see `k8s-leases`' database, a write in flight, or which thread the guard serves, and they enforce no GPU budget: the guard and `k8s-sweep` stay the budget's check.

### Scope

- **The webhook.** Each policy gets its own webhook entry, `validate.kyverno.svc-fail-finegrained-<policy>`, with `failurePolicy: Fail` and one match condition: the request is in the named pre-made namespace, and a ConfigMap only when it is the gate. The API server evaluates it, so a namespace that is not named never reaches Kyverno, even while Kyverno is down. The condition uses `has(request.name)`: the namespace controller's bulk delete carries no name, and a bare `request.name` is an error, which fails closed and leaves the namespace stuck Terminating (measured on kind).
- **The policy.** Namespaces `<prefix>*` that carry the opt-in label `dynamo.nvidia.com/agent-lease-fence: "true"`. Ops set the label by hand after the install check; the bot cannot label a Namespace.
- **The operations.** CREATE of Pods and of the bot's roots (DynamoGraphDeployment, Job, PodCliqueSet, Deployment, StatefulSet), and CREATE and UPDATE of the gate. Nothing else, so an outage never blocks a delete or a scale-down.

### Two policies, not one

On a policy's own webhook, Kyverno 1.15.2 evaluates the whole policy with the first rule-level failure action it finds (`pkg/webhooks/resource/handlers.go`, `engineresponse.go` `GetValidationFailureAction`). One always-enforcing rule would make every rule of the same policy enforce from install, and there would be no audit window. Measured on kind: with the canary as a rule of the main policy at `Audit`, a real unlabelled Pod was refused. So the canary is its own policy: it always enforces, and it refuses only the probe's canary Pod. The main policy is pure `Audit`, then `Enforce`. The main Pod rule excludes the canary-labelled Pod, so neither entry can answer for the other.

### The gate

ConfigMap `wcep-agent-fence` in the pre-made namespace holds `lease_id`, `state` (`active` or `closing`), `seq` and `uid`. `seq` is the claim's sequence from `k8s-leases`: strictly rising per namespace, never reused. The gate carries no lease label, and the guard never applies or deletes it (`fence_protected`).

The policy's rule for writes to the gate, compared only as numbers (`"07"` is `"7"`):

- only the bot writes it, with a numeric `seq`, a lease id, and a state of `active` or `closing`;
- `seq` never goes down; the same `seq` keeps its lease; the same `seq` may go `active` to `closing`, never back;
- a higher `seq` may set either state, and so may a create.

A DELETE is not the policy's: Kyverno 1.15.2 never denies a DELETE on a policy's own webhook (measured on kind; the same rule denies through the shared webhook). A deleted gate reads `missing`, which denies every Pod, so the failure is safe. The one residual: once deleted, the gate can be created again at any `seq`, because a create is not ordered. The guard's own check covers its writes: it opens only over a lower `seq`, and refuses `fence_mismatch` over a higher one.

**Open** (`ns claim`, after the plugin answers `active`): a create-only POST, or a JSON Patch that tests the version, `seq`, lease and state. A replay over an open gate writes nothing; a replay that answers `released` or `torn-down` writes nothing; a gate already `closing` for this lease and `seq` is left (teardown came first); a gate above this `seq` refuses `fence_mismatch`. If the write fails, the lease stands and no Pod can start: run `ns claim` again with the same terms. Every claimed write refuses `fence_not_open` until the gate reads `active` for its lease and `seq`.

**Close** (step 0 of claimed teardown): write `closing` for this lease and `seq` over anything below it, and read it back. So a claim that committed but never opened its gate still completes, and a late open of the same `seq` is denied.

What the Pod rule needs: the gate `active` for the Pod's lease label, and a controller owner. The rule skips a Pod labelled `wcep.agent/fence-probe: canary`, so the guard refuses any manifest that carries `wcep.agent/fence-probe` (`fence_probe_label_refused`): only the probe's dry-run Pods carry it. A Pod that lands after its roots are gone has a dangling owner, and the garbage collector removes it. The bot-root rule needs the gate `active` for the root's lease and a guard attempt stamp. A missing gate and a gate the policy could not read deny with different words (`Gate: missing`, `Gate: unreadable`).

### The probe and "armed"

`fence probe` sends two server dry-run Pod creates, with no GPU and tiny requests. They change nothing.

- **The canary Pod** (`wcep.agent/fence-probe: canary`): `finegrained` when the canary policy's own entry refuses it with rule `probe-canary`; `shared` when the shared entry refuses it; `engine_unavailable` on `failed calling webhook` naming one of our two entries; `disarmed` when admitted; `probe_failed` for any other refusal, which proves nothing.
- **The mode Pod** (a lease label no lease has): `enforcing` when the main policy's entry refuses it with `pods-need-the-open-lease`; `audit` when it is admitted with Kyverno's warning `policy agent-lease-fence.pods-need-the-open-lease: ...`; `absent` when admitted without it; `engine_unavailable` as above.
- **Armed** = the canary refused by the canary policy's own entry (or `engine_unavailable`), **and** the mode Pod refused by the main policy's own entry (or `engine_unavailable`). Both entries are needed: with either policy removed, the probe reads not armed (measured).
- **What it proves, and what it does not.** In Enforce the two refusals prove both entries exist and the main one enforces. An entry's name does not prove its expression: only the ops read at install does. In Audit the canary proves only its own entry; no probe proves the main entry, so the ops read is the only proof of it. During an outage, `failed calling webhook` names whichever of our entries the API server called first, so an outage proves one of our entries, not both.
- **Not armed carries its cause**, in words an operator can act on, into the teardown report, the plugin's refusal and its `fence_disarmed` flag. Both Pods admitted reads: no fence webhook answered, because the policies or the label are gone, or Kyverno has no webhook entries.

### The engine's failure modes

- **Down, entries in place** (a crash, a node drain; replicas stay 1): Pod and root creates and gate writes in the pre-made namespace are refused; nothing outside it is touched; a delete and a non-gate ConfigMap write succeed. The probe reads `engine_unavailable`: armed. Measured on kind.
- **Back after more than 100 s away**: Kyverno's watchdog empties its webhook list until its next reconcile. Measured on kind after 110 s away: the main entry was absent for about 10 s, starting 8 s after the pod was ready, and the probe read not armed through that window. A window shorter than the probe interval (5 s) would not be seen; the bot cannot read the webhook configuration to check (`references/bot.md`).
- **The admission controller scaled to 0** (or its Deployment deleted): Kyverno deletes every one of its webhook configurations (`pkg/utils/runtime/utils.go` `IsGoingDown`, `pkg/webhooks/server.go` `cleanup`). Measured: both entries gone, both probe Pods admitted, not armed. The entries come back when it scales up.
- **Ops switch to Audit, remove the label, or delete a policy**: denials stop; the probe reads not armed.
- **A cluster whose mutating Kyverno webhook covers every Pod create** (one of our clusters has such a policy, which defaults GPU Pods to a scheduler): during an outage the API server calls that webhook first, and the probe's answer names it, not ours: `probe_failed`, not armed, and the cause says Kyverno did not answer that webhook, so ours were not reached. Teardown then keeps the charge until Kyverno is back. That is the safe direction.

Every not-armed reading keeps the lease's GPUs charged: a fence that is not armed never completes a teardown.

### Claimed teardown with the fence

1. Probe. 2. Close the gate and read it back. 3. Resolve the open attempts and delete the lease's roots (as before). 4. Drain: wait `K8S_GUARD_FENCE_DRAIN_SECONDS` (120) of monotonic time from this run's own read of `closing`, probing at least every `K8S_GUARD_FENCE_PROBE_SECONDS` (5). 5. Read the inventory. 6. Probe again.

The cadence is checked, not assumed. The guard times every probe from the one before the close through the last, and treats any span from one probe's start to the next one's end longer than the interval plus 3 s as not armed (a fence state could have come and gone unseen). It reports the largest span as `max_probe_gap_seconds`, and the plugin refuses completion when it passes the plugin's own 8 s, which stays below the shortest emptied window measured (about 10 s): a larger limit would let that window pass unseen.

So the span must hold the calls between two probes at the clusters' real latency. A probe sends its two Pods at once, so it costs one call's time, and while teardown works (the close, the attempts, the deletes, the inventory) the guard probes after any kubectl call once 2 s have passed since the last probe began; in the drain, where nothing else runs, every 5 s. With `c` seconds per kubectl call, the worst span is the larger of 2 s + 2`c` and 3`c` while it works, and 5 s + `c` in the drain. **The design tolerates 2.5 s per kubectl call**, and a test pins a teardown with objects and an open attempt at that latency. Measured once on two of our clusters through Teleport, as the guard runs kubectl (a new process per call, so each pays its connection): p50 about 1.2 s, worst 1.64 s, of which about 1 s is the process and connection and 0.1 to 0.2 s the request itself. The 2.5 s is chosen against that measured worst of 1.64 s, not guessed, so do not raise the 8 s limit (the emptied window would pass unseen) or probe less often without measuring the clusters again. Above 2.5 s per call a claimed teardown cannot complete and keeps the charge.

It reports `complete` only when, besides the phase-1 conditions, the gate read back `closing` for this lease and `seq`, the drain ran its full time, every probe was armed, and no span between probes passed the limit. The report carries `fence`, `drain_monotonic_seconds`, `armed` (`continuous`, `trial` or `retired`), `lease_fence`, `probes` and `max_probe_gap_seconds`. A probe that is not armed ends the wait: `teardown_incomplete` with `fence_disarmed` and the probe, its cause included; the GPUs stay charged, and the next run starts with a new close and a new drain. A wall-clock step never ends the drain. The plugin checks the drain against its own 120 s, never the host's number.

The drain is a wait before the final check, not a timer that frees GPUs. A Pod admitted while the gate was open that lands after the drain is not seen; its owner is gone, so garbage collection removes it, and `k8s-sweep` flags it (`gpus_no_lease`). One probe is two dry-run creates, sent at once: one per claim, one every 5 minutes per live lease (the plugin's tick), and about 26 per teardown run, plus one per 2 s of kubectl work while teardown deletes and reads.

**Two ways out of a `fence_disarmed` strand.** Re-arm (ops restore Enforce, the label, the policies, or Kyverno's replicas): the next teardown completes. Or retire the fence for good: ops remove the label and both policies, the on-call engineer gathers the proof, and `k8s-guard down <premade> --lease <id> --fence-retired <proof URL>`, run on a host, records the proof with the plugin. It refuses `fence_not_retired` while the namespace carries the label or the canary reads anything but `disarmed`. It still closes the gate and waits the drain, needs no probe, and reports `armed: retired`. That `fence-retired` is refused from a bb thread rests on `BB_THREAD_ID`, which a shell can drop: a convention, not a boundary. The checks that bind are on the host (no label, the canary admitted); the human proof and the 24-hour stop-the-line are the owner's to keep.

### Trial claims and the audit window

`ns claim --audit-trial --gpus 1` asks the plugin for a trial: allowed only while the main policy is in Audit (the probe reads `audit` with scope `finegrained`), with the owner's first-run approval. Under a trial lease the guard refuses any Pod that requests a GPU (`trial_gpu_refused`) and every build (`trial_build_refused`): only a 0-GPU mocker runs. The trial's two steps: `fence trial close` closes the gate while the lease is active, and `fence trial delete-pod <pod>` deletes one of the lease's own Pods (lease label checked, UID and version preconditions, as an attempt), so its controller's replacement is a would-be denial the recorder must show. A trial teardown completes as `armed: trial` when every probe reads the policies present in Audit.

### The recorder

`fence record` (read only) reads the namespace's PolicyReports and Kyverno's Events naming `agent-lease-fence`, and appends rows it has not seen to `${XDG_STATE_HOME:-~/.local/state}/k8s-fence/<cluster>.jsonl`. A user timer, `k8s-fence-record@<context>.timer`, runs it every 15 minutes, inside the Events' one-hour lifetime; chezmoi never enables it. Rows hold only fields those shapes hold: no user, no operation. Probe rows are left out (by name and by the canary policy). Its output counts the probes this host sent, by rule, for the tally. Measured on kind in Audit: a would-be denial leaves a PolicyViolation Event on the Pod in the namespace, and so does each dry-run mode probe; dry runs count in `kyverno_policy_results_total` (`dry_run="true"`). It cannot see an object made and deleted between two runs whose Event also expired; ops read the counter at the end of the window for that.

**The tally.** At the end of the window ops read `kyverno_policy_results_total` for `agent-lease-fence` and `agent-lease-fence-canary`, by rule and result, for the time since the admission pod last started: the counter lives in memory, and a restart zeroes it. Measured on Kyverno 1.15.2 in Audit (the kind fixture pins these numbers, so a Kyverno change shows there first), each request the Pod rule fails moves its counter by 2, because the admission and the audit evaluations each count, and leaves 2 Events; each probe moves the canary's counter by 1. `fence record` counts a probe for a rule only when Kyverno evaluated it (the canary for `finegrained` or `shared`, the Pod rule for `audit` or `enforcing`): a probe sent in an outage, with the entries emptied, or refused for another reason moves no counter. So, with `P` those probes since that start, summed over every probing host (`k8s-guard fence record --since <start>` `probes_sent`), and `R` the distinct objects among the recorder's rows for the Pod rule since then:

- `pods-need-the-open-lease` `fail` = 2 × (`P` + `R`);
- `probe-canary` `fail` = `P`;
- the bot-root rule's `fail` = its recorder objects (not measured on kind; a trial shows it).

A trial's planned denials are among the recorder's rows and are not added again. With more than one admission replica the counter lives in each pod: sum it over the pods, and count from the latest pod start. Any difference resets that cluster's window.

### Where a denial shows, and the off-switch

Every message names the policy, the owner, the contact and where the off-switch is written; the policies' descriptions hold the full text. A denial shows in the client's error, in the `FailedCreate` Event on a controller's owner, in Kyverno's violation Event and in its `blocking admission request` log line; never in a report, because a denied object never exists. The off-switch, in order: set the main policy to Audit; remove the label; delete both policies. Each stops denials, and each keeps a live lease's GPUs charged until the owner is told. Scaling the admission controller to 0 also disarms the fence, for every Kyverno policy on the cluster.

### The Kyverno CLI

The CLI suites (`policy/tests/`, run by `test/test-k8s-fence.py`) mock the gate; the kind fixture (`test/kind-fence.sh`) runs the real read. The v1.15.2 CLI has three defects these tests work around: it panics on a policy with webhook match conditions (so the suites run on the policies without them); it counts an expected `result: fail` of a validate rule as passed whatever the policy did; and it writes `Excluded` for a resource no rule answered, whatever was expected. So the test reads every row's reason, and each suite is run against broken policies it must fail.

## Apply

The rules in this section and the next are pure functions in `scripts/k8s_guard_rules.py`.

The guard parses no YAML. `kubectl label --local` (no API call) reads the file and labels every object with `wcep.agent/namespace-uid=<namespace UID>` and, when the write runs under a lease, `wcep.agent/lease=<lease id>`. Every object must carry both stamps. Then, for each object, the guard reads it and plans one write (see "Ownership on the mutation"). It sends that exact request with `--dry-run=server`, then sends it for real. Every check runs on the JSON from the dry run, so defaults and webhook changes are checked too. A write that meets a changed object makes the guard read, check and budget everything again, at most three times; then it refuses `write_conflict`.

## Ownership on the mutation

A read before a write proves nothing about the object the write reaches. So every write carries its own checks to the server:

- an absent object gets a create-only POST (`kubectl create -f -`). If anything took the name meanwhile, the server answers `AlreadyExists`, and the guard reads the object again: one that is not ours is left alone (`object_not_yours`);
- a present object must be ours: in a claimed namespace, labelled with this lease; in a namespace the guard created, labelled with this lease, or with this namespace UID and no lease. It gets a JSON Patch whose first operations test its UID, its `resourceVersion` and that label, before the edits. A merge patch (`kubectl apply`) can leave out an unchanged `resourceVersion`, so `apply` is never used to write. A patch never adopts an object with no stamp, since a controller may own it;
- every delete keeps the UID the object was listed or read with as its identity. Before each send, not only after a conflict, the guard reads the object again and requires that UID and its lease label (a delete cannot test a label). Then it sends the delete with that UID and the `resourceVersion` just read as `DeleteOptions.preconditions`. A fresh read is never taken as the identity, so a relabel or a replacement, before the read or after it, defeats the delete. On a conflict it reads and checks again, a bounded number of times;
- scale to 0 tests the UID, the `resourceVersion` and the lease label.

The patch replaces each field the manifest gives under `spec` on its own, so fields the server set (a Service's `clusterIP`) stay. It does not remove a field the manifest leaves out.

## Write attempts

A write whose reply never came can still land. A slot that expired, a client that exited, or a list that reads empty proves nothing about it. So every write under a lease is an attempt with its own id:

1. The guard records the attempt in `k8s-leases` (`bb k8s lease attempt begin`, with the slot's token) before it sends the write: the verb, the object, and for a patch or delete the UID and `resourceVersion` its precondition carries.
2. Creates and patches carry the id in the annotation `wcep.agent/attempt`.
3. The attempt is resolved by the server's answer (success, or an outright refusal such as `AlreadyExists`, `Conflict` or `Invalid`), by seeing its id on the object, or, for a patch or a delete, by proof that its precondition can no longer hold: the object's UID or `resourceVersion` has moved on. A create's only precondition is that the name is free, and that can hold again, so a create resolves only by the server's answer or by its own stamp. A create that timed out stays open while the name reads 404, and also while another object holds the name: once that object goes, the create can still land. A timeout, a lost connection or an internal error leaves the outcome unknown.
4. An open attempt blocks the lease's next write. Before it takes the slot, the guard tries to resolve every open attempt from storage, and refuses `attempt_unresolved` while one stays open. A write whose outcome stays unknown refuses `write_unresolved`. Teardown cannot complete while an attempt is open, and the plugin keeps the GPUs charged. There is no timer: only seeing the write's outcome resolves it.

- **Kinds:** `DynamoGraphDeployment` (`nvidia.com/v1alpha1` or `v1beta1`), `ConfigMap`, and `Service` of type `ClusterIP`. Each object must be in the `-n` namespace.
- **DGD fields:** only the fields the guard can count. Refused: scaling adapters, enabled autoscaling, planner, EPP and LPX components, `roles`, DRA resource claims, any GPU resource other than `nvidia.com/gpu`, PVCs the DGD would create, and any field the guard does not know.
- **Pod specs:** no `hostPath`, `priorityClassName`, `nodeName`, `schedulerName`, privileged containers, `hostNetwork`, `hostPID` or `hostIPC`, and no toleration with `operator: Exists` and no key. Every container needs `cpu` and `memory` in both requests and limits.
- **Name:** a DGD name has at most 39 characters. An object may appear only once in a file.
- **GPU type:** every GPU pod needs `nodeSelector` `nvidia.com/gpu.product` equal to the profile's `gpu_product`, the GPU the lease counts.

## Build Jobs

A build is a Kubernetes `Job` in the claim's namespace, rendered by the guard from one fixed template. No caller writes the manifest, so the fields the plan fixed cannot be chosen: the registry `nvcr.io/goirlvsxnepa/llm_nim`, the pinned images, the Job's deadline and retention, and the resources all come from `scripts/k8s_guard_rules.py`.

`build run` requires this thread's live claim (the pre-made namespace's lease) and the two secrets the Job mounts: `nvcr-push` and, for an ssh source, `git`. It prints the Job's name and nothing else. One Job is:

- **Image and tag.** `<image>:<commit's first 12 hex>-<arch>`, registered under `nvcr.io/goirlvsxnepa/llm_nim`. The clone gets the **full 40-hex** commit and verifies `git rev-parse HEAD`; the build container pushes exactly `type=image,name=<registry>/<image>:<12 hex>-<arch>,push=true`. A tag is never `latest`, and a name already taken is refused (`job_exists`): `build show` it, `build delete` it, then submit again.
- **Arch.** The profile's `arch`; `--arch` may only name it (`arch_mismatch` otherwise), and the pod's `nodeSelector` is `kubernetes.io/arch: <arch>`. amd64 builds on `dynamo-aks-dev`, arm64 on `dynamo-gcp-dev-02`.
- **Pod.** `restartPolicy: Never`, `backoffLimit: 0`, `activeDeadlineSeconds: 3600`, `ttlSecondsAfterFinished: 86400`; `runAsUser`/`runAsGroup` 1000 and `fsGroup` 1000; `seccompProfile` and `appArmorProfile` `Unconfined`; `allowPrivilegeEscalation` **not set** (false sets `no_new_privs`, which blocks `newuidmap`). Every container requests and limits `cpu`, `memory` and `ephemeral-storage`.
- **Init containers, in order.** `tag-check` asks the registry whether the tag exists; `clone` runs `git clone --filter=blob:none "$REPO" /src`, checks out `$COMMIT` and tests `git rev-parse HEAD`. The clone runs the pinned `moby/buildkit:v0.23.2-rootless` digest, which has `git`, `openssh` and a `user` at UID 1000; it sets **its own** `command`, because the image's entrypoint is `rootlesskit buildkitd` and would otherwise start a daemon instead of cloning. `tag-check` runs the pinned crane debug image, whose busybox shell runs the probe.
- **The build.** `buildctl-daemonless.sh build --oci-worker-no-process-sandbox --frontend dockerfile.v0 --local context=/src --local dockerfile=/src/<dirname> --output ...`, with BuildKit's state in an `emptyDir` at `/home/user/.local/share/buildkit`.
- **The push credential.** Secret `nvcr-push`, made with `secret ngc-push --exposure-accepted`, mounted read-only at `/home/user/.docker`, with `items: [{key: .dockerconfigjson, path: config.json}]` (a `dockerconfigjson` Secret's key is `.dockerconfigjson`, and BuildKit reads `config.json`; without the rename it gets a 401). `DOCKER_CONFIG=/home/user/.docker`. No `imagePullSecrets`: the images are public and only the push credential and the git key are secrets.

**The tag check runs inside the cluster** because the host has neither the credential nor a registry client. It is the plan's pinned crane, in its `:debug` variant: `gcr.io/go-containerregistry/crane/debug:v0.20.3` is the same binary as the release image plus a busybox shell at `/busybox`, which the probe needs to turn crane's answer into the init container's. `crane digest` sends every manifest `Accept` type itself - the OCI index, the OCI manifest, the Docker list and Docker v2 - so a 404 means the tag is missing, not that an OCI index went unaccepted; its stderr carries `MANIFEST_UNKNOWN` for a missing tag, or `NAME_UNKNOWN` for a repository that does not exist yet, which cannot hold the tag either. The probe reads the push credential from `DOCKER_CONFIG` and maps crane's answer:

| crane's answer | Marker | Result |
|---|---|---|
| exit 0: the tag is there | `tag_exists` | the init container fails, so the build never runs |
| the error says `UNAUTHORIZED`, `DENIED`, 401 or 403 | `registry_denied` | refused |
| the error says `MANIFEST_UNKNOWN` or `NAME_UNKNOWN` | (silent) | the clone and the build run |
| anything else, including no answer at all | `registry_error` | refused, rather than guessing |

`build show` reports the marker as the refusal's code. An existing tag is never overwritten: a DGD may already pull it. The first real build confirms the probe's registry flow (`references/bot.md`, section 8 of the runbook).

**The git key and the pinned host key.** `secret git --repo <owner>/<repo>` names the ref by convention, `op://Fleet/<owner>-<repo> deploy key/private key`, fetches it with `fleet secret get` itself (no value passes through a caller, argv, a file or a pipe from outside), and creates:

- Secret `git` with key `id_ed25519` (the 1Password field is `private key`; a Secret key cannot contain a space), stored with exactly one trailing newline, because ssh refuses a key whose last line is unterminated;
- ConfigMap `git-known-hosts` with key `known_hosts`, holding GitHub's published ed25519 host key.

The `private key` field must hold the **OpenSSH** private-key form (`-----BEGIN OPENSSH PRIVATE KEY-----`), the one `ssh-keygen -y` reads. A PKCS#8 `BEGIN PRIVATE KEY` fails inside the clone, and the guard does not convert: `fleet` refs cannot carry a format query, so the item itself must be the OpenSSH form.

For an ssh `--repo`, the Job projects both into one volume at `/home/user/.ssh`, read-only, with `defaultMode: 0400` (the kubelet materializes a group-readable copy for `fsGroup` 1000, which is the mode the clone container actually reads). The clone sets `GIT_SSH_COMMAND` with `-i /home/user/.ssh/id_ed25519`, `IdentitiesOnly=yes`, `UserKnownHostsFile=/home/user/.ssh/known_hosts` and `StrictHostKeyChecking=yes`; there is no `accept-new`, so a changed host key fails the build instead of being trusted. A writable mount, a mount outside `/home/user/.ssh`, any mode but `0400`, a volume that is not projected, a clone that is not the pinned builder image or sets no command, and an ssh option that accepts a new host key are all refused. An `https` source runs the same Job without the mount.

**The charge.** The Job requests no GPU, but its claim charges one (`ns claim` cannot ask for zero). The ledger line records the claim's charge, never the Job's zero.

**Teardown and the index.**
- `build index` runs the pinned `crane` image as three sequential steps, `crane index append` (both per-arch tags on the commit's tag), `crane manifest` and `crane digest`. It needs the same live claim; the plan runs it under the amd64 claim before that claim is released.
- A Job delete carries `propagationPolicy: Background`; without it the Job's Pods are orphaned and, carrying the lease label, hold the claim's teardown open. Claimed teardown deletes a lease's Jobs, and its re-read lists them.
- The push secret exists for the claim's length: it is lease-labelled, claimed teardown deletes it, and the next claim's build creates it again. Run `secret ngc-push --exposure-accepted` per claim, after the owner's acceptance of the exposure is recorded.

**What a caller may not do.** The template refuses, before any write:

- `hostPath`, `priorityClassName`, `nodeName`, `schedulerName` (the server's own `default-scheduler` is accepted on the dry-run answer only), `hostNetwork`, `hostPID`, `hostIPC`, privileged containers and `allowPrivilegeEscalation`;
- any container without `cpu`, `memory` and `ephemeral-storage` in both requests and limits;
- an image without `@sha256:` digest;
- a pod-template label without the lease (`lease_label_required`);
- a Job whose output tag is not the commit's `<12 hex>-<arch>`, or does not push (`invalid_tag`);
- an arch other than the profile's (`arch_mismatch`), a commit that is not 40 hex (`invalid_commit`), an image name it cannot tag (`invalid_image`).

## Dev Jobs

`dev run` needs the pre-made namespace, a live claim with an open fence gate, and at least 900 seconds left on the lease. Trial claims refuse with `trial_dev_refused`. Create `nvcr-pull` with `secret ngc`. For an SSH source, also create `git` and `git-known-hosts` with `secret git`.

The guard pins the image at `nvcr.io/goirlvsxnepa/llm_nim/<image>:<image-commit's first 12 hex>-<arch>@<digest>`. The arch must match the cluster profile. The clone checks out the full `--commit` into `/work/src`, where the dev container runs `/usr/bin/tini -- sleep infinity`. Only the clone mounts the SSH key. The Pod disables service account token automount and uses no GPU or push secret.

The Job and its Pod template carry the lease label and `wcep.agent/build: dev`. The Job name ends with the first six hex digits of the write attempt. Its deadline is the lease time left at launch and stays fixed after a lease extension. Kubernetes may remove the finished Job after 3,600 seconds.

The guard checks the fixed Job template before submission and checks the server's dry-run response. It accepts the listed Kubernetes defaults and refuses other changes to the template. These checks cover the Job's Pod template. The Kubernetes controller creates the Pod in a separate admission request.

An unfinished dev Job under the same lease causes `dev_exists`. Run `dev show <job>` to read its state. To stop it, run `delete job/<job>`. You can relaunch the same commit once its Job finishes. Claimed teardown deletes the lease's dev Jobs and waits for their Pods to leave.

## Budget

A DGD's allocation is the larger of two numbers: its spec (replicas times nodes times GPUs per pod) and the GPUs its pods still request. Terminating pods count. Finished pods do not. GPU pods that belong to no DGD count on their own. The namespace's allocation is the sum.

`kubectl` writes the objects of a file one at a time and goes on after a failure, so any subset of the file may land. So each DGD in the file counts at the larger of its old and new spec: a shrink that fails never pays for a growth. Shrink in one apply, then grow in the next.

With no lease record, that allocation must be 0. With a record, the guard:

1. reads the lease. It must be `active` and match this thread, the profile name, the namespace, its UID, and the profile's GPU type;
2. takes the lease's slot (`bb k8s lease op begin`);
3. runs the dry run and every check, and reads the DGDs and pods again;
4. refuses when the allocation is more than the lease;
5. refuses when any pod template of a new DGD lacks exactly `wcep.agent/lease=<id>` (`lease_label_required`): every v1alpha1 service and v1beta1 component, GPU or not, zero replicas and multi-node included, because teardown finds the lease's pods by that label. Without `multi_node` it also refuses when the allocation is more than one node of the profile, and when a GPU pod lacks a required pod affinity to that label on `kubernetes.io/hostname`;
6. reads the namespace UID once more, then writes the objects one request at a time, each as an attempt (above). Before each one it refuses when less than 40 seconds of the slot are left, and it stops `kubectl` before the slot ends. Objects already applied stay; the budget above counts every subset;
7. calls `op end`. When it reports the lease as no longer `active`, the guard runs `expire` at once and exits with `lease_revoked`.

Every write in a namespace with a lease record takes the slot, GPU or not. `secret` and `delete` follow the same rule.

## Teardown authority

In a claimed namespace only the first rule below applies.

`down` by the owner releases an active lease first. Any other `down`, and every `expire`, needs one of these:

- the plugin says the lease is `revoking` or `releasing`, for this cluster, namespace and UID;
- the plugin cannot be reached, `--uid` matches the namespace, and the namespace's expiry has passed.

A JSON error from the plugin, such as an unknown lease, is an answer, not an outage, and is refused. So is a lease the plugin reports as `released` or `torn-down`. `down` waits for GPU pods only for a bounded time, then deletes the namespace in any case:

- deleting the namespace stops every pod still in it;
- a shared-model-cache PVC can stay `Terminating` until its pods are gone, which is expected;
- `k8s-leases` keeps the namespace's GPUs charged until a host reports the namespace gone, so the budget stays correct while pods finish.

`down` does not match pods to DGDs: names can be reused, and pods carry no reliable link to their DGD.

Teardown selects the DGDs of the lease it is tearing down: `wcep.agent/lease=<lease id>`. It never selects by the namespace UID. In a long-lived namespace, every task that ever applied there shares that UID, so a selector on it picks a colleague's DGD too. The UID stays an instance check: the namespace is read again, and each DGD's own UID and `resourceVersion` are the fences its delete carries.

The lease id is the effective one: `--lease` when the caller passes it, else the namespace's lease record. The plugin's own lease state is the authority; the record is the guard's copy. With no lease id at all there is no label to select by, so teardown deletes no DGD and reports the reason as `dgds_skipped`. `down` still deletes the namespace, which ends everything in it. A DGD with no lease label, such as one an earlier task applied, is never selected: `expire` leaves it running, and `down` still ends it with the namespace.

## A re-created namespace

A namespace name is never made twice: `ns create` adds a random suffix and refuses a name that exists. So no namespace can be re-created under a name an earlier one had, through the guard. The checks below stay as defense in depth.

One residual remains, and the owner accepted it. All three of these must happen at once:

- another thread uses the same issue number and purpose;
- its suffix collides with a deleted namespace's suffix, about 1 in 3.7e15;
- the owner races its own `apply` against its own `down`.

Then the late `apply` can land in the new namespace. Its DGD carries the deleted namespace's lease label, so `expire` of the new namespace never touches it. `inspect` counts it, so `k8s-leases` sees the over-allocation, revokes the lease and flags it. At the grace deadline, `down` deletes the namespace after its bounded wait, so the stray DGD and its pods go with it. The namespace delete carries the UID precondition, so nothing outside that namespace is touched.

`k8s-leases` sends `expire`, `down` and `extend` with the UID it stored, without reading the namespace first. The guard refuses a `--uid` that differs from the namespace, before any write.

A read cannot fence a later write, so the writes carry their own fences, which the API server checks:

- `expire` and `down` select only DGDs labelled with the lease being torn down. A namespace re-created under the same name holds no DGD of that lease.
- Each `expire` patch tests the DGD's UID, its `resourceVersion` and its lease label, so a DGD that lost the label is refused, not scaled.
- Each DGD delete reads the DGD again and checks that its UID is still the listed one and that its lease label holds before sending, then carries the listed UID and `resourceVersion`: a delete has no label test, so the version is what refuses a change in place, and the UID refuses a replacement with the same name. On a conflict it reads and checks again, a bounded number of times, so a status write does not stop a teardown that is still the lease's. The namespace delete carries the namespace UID.
- `extend` replaces the lease record under its `resourceVersion` and never creates one.

The guard also reads the UID again before each write, so most races stop before a request is sent.

## Ledger

Every write appends one JSON line to `${XDG_STATE_HOME:-~/.local/state}/k8s-guard/ledger.jsonl` on the host. When `BB_THREAD_STORAGE` is set, it also appends the line to `$BB_THREAD_STORAGE/k8s-ledger.jsonl`. A line holds the time, the calling thread, the cluster, the context, the namespace, its UID, the object, the GPUs, the direction (`up`, `down` or `extend`), the lease, and the issue. No secret value reaches stdout, stderr, or a ledger.

Read a `down` line's `gpus` as the allocation that down ended: the lease's DGDs, or, when it had no lease id to select by, every DGD in the namespace, which the namespace delete ended with it. `gpu_pods_left` counts the GPU pods still there when the wait ran out.

`k8s-sweep --enforce` calls `expire` and `down` with teardown authority, never as the owner. After each call it appends a line with `object: Sweep/<ns>` and `direction: enforce` to the host ledger. The guard's ledger reader ignores that direction (`references/k8s-sweep.md`).

`dgds_kept` is in the command's output, not the ledger: it names the DGDs of the lease that changed between the read and the delete, so this down did not delete them. They went with the namespace in the same call, or, for `expire`, they were never scaled.

## Not proven without a cluster

The tests use fakes. The first run on a cluster must confirm these, and run `selftest fences` in its smoke namespace:

- the bot cannot create a namespace on our clusters (`namespaces is forbidden ... at the cluster scope`), so `ns create` refuses `namespace_create_denied`; where a cluster grants it, it can create a namespace with labels and annotations, and delete it;
- a JSON Patch `test` on `/metadata/resourceVersion` is refused with `testing value` when the version moved, and a `resourceVersion` in `DeleteOptions.preconditions` is refused with `Precondition failed`;
- `kubectl get <resource> -l wcep.agent/lease=<id>` works for each kind the claimed teardown lists, and a resource the server does not serve fails with `doesn't have a resource type`;
- `kubectl label --local` writes both stamps in one call, and the server dry run returns them on every object;
- the API server accepts the `DeleteOptions` body that `kubectl delete --raw <path> -f -` sends without a `Content-Type`;
- the dry run of a stock DGD adds no field the guard refuses;
- on a Job's server dry run, the API server defaults `schedulerName: default-scheduler` on the pod template (the guard admits that one value on the dry-run answer, and no other);
- the crane debug image at `v0.20.3` by digest exists in both architectures and its `/busybox/sh` runs the probe;
- `crane digest` on `nvcr.io` answers a missing tag with `MANIFEST_UNKNOWN` on stderr and exit 1, and an existing one with exit 0; a missing repository answers `NAME_UNKNOWN` or `MANIFEST_UNKNOWN`, and both are absent; `v0.20.2`, named by the plan, is no longer served by `gcr.io/go-containerregistry/crane`;
- every container's `ephemeral-storage` request and limit are accepted together with the node's allocatable storage;
- the bot may `create jobs.batch` in the pre-made namespace.
- the bot's username as the API server sees it through Teleport (the policies name `bot.user`; a trial's bot-root rule must read `pass`, not `skip`);
- that the Dynamo operator (both DGD versions) and Grove copy the lease label onto Pods and give them a controller owner (a trial's Pod rule must read `pass`); the Grove multi-node path is first proven by the first enforced GPU deploy, where a mismatch refuses it;
- that Kyverno emits a PolicyViolation Event for each Audit would-be denial on the managed clusters (measured on kind);
- each cluster's support for webhook `matchConditions`, and the drain against its real request timeout;
- the bot's read of PolicyReports and Events in its namespace;
- that no other admission check (Pod Security admission, or another policy on the shared webhook) refuses a probe Pod before ours answers: the probe would read `probe_failed`, so claims refuse and teardown keeps the charge, and with webhooks answering in parallel the reading could change from one probe to the next. A trial shows it.

## Tests

```bash
timeout 600 python3 test/test-k8s-guard.py </dev/null
timeout 120 python3 test/test-k8s-first-build.py </dev/null
KYVERNO_CLI=~/.local/bin/kyverno timeout 300 python3 test/test-k8s-fence.py </dev/null   # Kyverno CLI v1.15.2
timeout 2400 bash test/kind-fence.sh </dev/null   # the dev host only: kind, helm and the chart images
```
