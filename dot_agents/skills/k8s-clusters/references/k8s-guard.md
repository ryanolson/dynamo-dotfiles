# k8s-guard

`k8s-guard` is the only way agents write to a shared cluster. It wraps `kubectl`, runs as the agents' bot, and refuses any write outside our namespaces, our kinds, or our GPU lease. The cluster runbook in Linear says when to use each command. This file is the reference for what the guard checks.

Every command prints one JSON object on stdout. A refusal exits non-zero with `{"error": "...", "code": "..."}`. The code is stable; the message is for people.

## Commands

| Command | Who may run it | What it does |
|---|---|---|
| `ns create <purpose> --issue <n> --hours <h>` | a bb thread | Creates `<namespace_prefix><n>-<purpose>-<suffix>`, where the suffix is 10 random letters and digits from the CSPRNG, with the opt-in labels, `owner`, `wcep.agent/thread`, `wcep.agent/issue`, and the annotation `wcep.agent/expires-at`. `h` is at most 4. A name that exists is refused, and a name longer than 63 characters. It records the name and UID in the thread's ledger. Where the bot may not create a namespace (Forbidden at the cluster scope, as on our clusters), it refuses `namespace_create_denied` and names `ns claim`. |
| `-n <premade> ns claim --gpus <n> --hours <h>` | a bb thread | Claims the profiles' pre-made namespace (`[bot] premade_namespace`) with a `k8s-leases` lease. See "A claimed namespace" below. It writes nothing to the cluster. |
| `-n <ns> lease acquire --gpus <n> --hours <h>` | the namespace's thread | Calls `bb k8s lease acquire` with the profile name, the namespace UID, the thread, and the profile's GPU type. Writes the lease record (below). Prints the pod label and pod affinity that GPU pods must carry. |
| `-n <ns> secret hf\|ngc\|ngc-push` | the namespace's thread | Creates the Secret from `fleet secret get`, on stdin only, with `kubectl create -f -`: create-only, labelled with the namespace UID and the lease. `ngc` is the pull-only key (`op://Fleet/NGC Pull Key/credential`); `ngc-push` is the push credential and is never used for a pull secret. `ngc-push` also needs `--exposure-accepted`, and refuses `push_not_approved` without it: the value is readable by everyone in the access group once it is in the namespace. The flag is a stated step, not an authority check. Prints only the secret's name. |
| `-n <ns> secret git --repo <owner>/<repo>` | the namespace's thread | Creates the read-only deploy key as Secret `git` (key `id_ed25519`) and the pinned GitHub host key as ConfigMap `git-known-hosts`, both from the guard, both lease-labelled. The ref is the convention `op://Fleet/<owner>-<repo> deploy key/private key`. The key is never printed. It refuses `secret_unavailable` when the item is absent; there is no fallback. |
| `-n <ns> build run --repo <url> --commit <40 hex> --dockerfile <path> --image <name> [--arch amd64\|arm64]` | the namespace's thread, under its live lease (the claim in the pre-made namespace) | Renders one image-build Job from the fixed template (see "Build Jobs"), takes the lease's slot, records one ledger line and prints `{"job": "<name>"}` only. Refuses a commit that is not 40 hex, an image name it cannot tag, an arch that is not the profile's, a forbidden field, a Job without the lease label on its pod template, and a name already taken (`job_exists`). |
| `-n <ns> build show <job>` | anyone | Read only. Prints the Job's state and the last 200 log lines, with credential-shaped strings redacted. A failed Job exits non-zero with `build_failed`, `tag_exists`, `registry_denied` or `registry_error`. |
| `-n <ns> build index --commit <40 hex> --image <name>` | the namespace's thread, under its live lease | The manifest-list Job: `crane index append -t ...:<12 hex> -m ...:<12 hex>-amd64 -m ...:<12 hex>-arm64`, then `crane manifest` and `crane digest`. Runs under the still-live amd64 claim. |
| `-n <ns> build delete <job>` | the namespace's thread, under its live lease | Deletes the Job with `propagationPolicy: Background`, so its Pods go with it. |
| `-n <ns> apply -f <file>` | the namespace's thread | Applies DGDs, ConfigMaps and ClusterIP Services, after the checks below. |
| `-n <ns> delete <kind>/<name>` | the namespace's thread | Deletes one DGD, Job, ConfigMap, Service, Secret, PVC or PodCliqueSet, with its UID and `resourceVersion` as preconditions. A Job's delete carries `propagationPolicy: Background`. An object labelled with another lease is refused (`object_not_yours`); in a claimed namespace, so is one without this lease's label. The lease record, the model cache PVC and the pull secret are refused. Like `apply`, it takes the lease's slot, or needs a namespace that holds no GPUs. |
| `down <ns> [--uid <uid>] [--lease <id>]` | the namespace's thread, or anyone with teardown authority | Releases an active lease first (owner only). Deletes each DGD labelled with the lease being torn down: it reads the DGD again, checks that its UID is still the one the list gave and that its lease label still holds, then deletes it under that listed UID and the `resourceVersion` just read. A DGD that changed in between is left (`dgds_kept`), not deleted. Waits, at most `K8S_GUARD_DOWN_WAIT_SECONDS` (300), for every GPU pod in the namespace to go. Then deletes the namespace with its UID as a precondition, whatever is left, and reports `gpu_pods_left`. A namespace already being deleted gets no second delete (`terminating: true`). With no lease id (no `--lease` and no record) it deletes no DGD and says so in `dgds_skipped`. In a claimed namespace it never deletes the namespace: see "A claimed namespace". |
| `expire <ns> [--uid <uid>] [--lease <id>]` | anyone with teardown authority | Sets every replica count of every DGD labelled with the lease being torn down to 0. With no lease id (no `--lease` and no record) it scales nothing and says so in `dgds_skipped`. |
| `extend <ns> [--uid <uid>] [--lease <id>]` | anyone | Copies the lease's `expires_at`, unchanged, into the lease record. It only replaces the record `lease acquire` wrote, under the `resourceVersion` it read, and never creates one. The lease must be `active` and match the cluster, the namespace and the UID. It never moves the stored expiry earlier, so a late call cannot undo a newer extension. |
| `-n <ns> selftest fences` | the namespace's thread | For the first run on a cluster. On the first DGD the guard applied, it sends each teardown fence once with a wrong UID, a stale `resourceVersion` or a wrong lease label: three JSON patches, two DGD deletes, a namespace delete (not in a claimed namespace, which gets no Namespace request at all), and a lease record replace. Each payload changes nothing even if the server ignored its fence. It prints each server response and reads the objects again. It fails with `fence_failed` unless every request was refused for its fence's own reason (`testing value`, `Precondition failed`, `the object has been modified`) and nothing changed. Another error, such as Forbidden, proves nothing. |
| `inspect <ns>` | anyone | Read only. Prints `uid`, `thread`, `expires_at`, `dgd_gpus`, `pod_gpus`, `gpu_type`, `gpu_pods_left`, and also `allocation`, `lease_id`, `developer` and `agent_rbac` (the Namespace's two opt-in labels). A missing namespace gives code `not_found`; any other failure gives another code. |

Every command needs `--context`. `-n` can come anywhere.

The commands for the namespace's thread (`lease acquire`, `secret`, `apply`, `delete`, `selftest fences`, and the owner's `down`) take the namespace from the thread's ledger, `$BB_THREAD_STORAGE/k8s-ledger.jsonl`. A given `-n` must be a namespace this thread created, and its UID must still be the one recorded. Without `-n`, the thread's only live namespace is used. The pre-made namespace is different: see "A claimed namespace". Everything else is refused, including `scale`, `patch`, `edit`, `replace`, `set`, `label`, `annotate`, `get`, and `-A`.

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
- Every owner command in the pre-made namespace needs this thread's claim, and the plugin's lease must be `active` and name this thread, cluster, namespace, UID and GPU type. The ledger line only points at the lease. With no claim there are no writes, CPU-only included (`namespace_not_claimed`). `wcep.agent/thread` is never read there.
- Teardown takes only the plugin's word (`revoking` or `releasing`). The namespace keeps no expiry, so there is no offline teardown; with the plugin unreachable, `down` and `expire` refuse `lease_unavailable`.
- The owner's `down` releases the lease and returns. The plugin then runs `down` on a host, which:
  1. resolves the lease's open write attempts from storage (below);
  2. scales the lease's DGDs to 0, then deletes each object labelled `wcep.agent/lease=<id>` of the kinds DGD, Job, PodCliqueSet, Service, ConfigMap, Secret and PVC (not the model cache PVC or the pull secret), each with its UID and `resourceVersion` as preconditions. A Job's delete carries `propagationPolicy: Background`, because a `batch/v1` Job delete otherwise orphans its Pods, which carry the lease label and would hold teardown open for the Job's 24-hour retention;
  3. reads the lease's labelled objects again, Jobs included, adding pods, PodCliques, Deployments and StatefulSets. Terminating pods count;
  4. reports `complete` only when every attempt is resolved, nothing changed hands, every list was read, and nothing of the lease is left. Otherwise it refuses `teardown_incomplete`, with what it resolved and what is left.
- `down` never deletes, labels or patches the pre-made namespace. The plugin frees the lease's GPUs only through one transaction conditional on that completion (`k8s-leases` README). Running `down` again after a crash goes on from where it stopped.
- A pod with the DGD's name label but not the lease label is never selected, waited for or deleted.

Claims stay off until the admission webhook that counts controller writes exists: until then `k8s-leases` refuses every claim with `enforcement_unhealthy`.

Install order: this guard calls `bb k8s lease attempt begin` on every leased write, in a namespace it created too, and `bb k8s lease latest` for a claim. A `k8s-leases` older than the claim protocol answers those with usage text, which the guard reads as an unreachable plugin, so every leased write would refuse `lease_unavailable`. Install the plugin first, then let hosts take this guard (`chezmoi update` or `chezmoi-headless-update`). The older guard works with the newer plugin.

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
- **Init containers, in order.** `tag-check` asks the registry whether the tag exists; `clone` runs `git clone --filter=blob:none "$REPO" /src`, checks out `$COMMIT` and tests `git rev-parse HEAD`. Both run the pinned `moby/buildkit:v0.23.2-rootless` digest, which has `git`, `openssh` and a `user` at UID 1000. The clone sets **its own** `command`: the image's entrypoint is `rootlesskit buildkitd` and would otherwise start a daemon instead of cloning.
- **The build.** `buildctl-daemonless.sh build --oci-worker-no-process-sandbox --frontend dockerfile.v0 --local context=/src --local dockerfile=/src/<dirname> --output ...`, with BuildKit's state in an `emptyDir` at `/home/user/.local/share/buildkit`.
- **The push credential.** Secret `nvcr-push`, made with `secret ngc-push --exposure-accepted`, mounted read-only at `/home/user/.docker`, with `items: [{key: .dockerconfigjson, path: config.json}]` (a `dockerconfigjson` Secret's key is `.dockerconfigjson`, and BuildKit reads `config.json`; without the rename it gets a 401). `DOCKER_CONFIG=/home/user/.docker`. No `imagePullSecrets`: the images are public and only the push credential and the git key are secrets.

**The tag check runs inside the cluster** because the host has neither the credential nor a registry client. It is a shell probe in the same pinned builder image, because the pinned `crane` image is distroless and has no shell, and `crane digest` cannot turn "the tag exists" into the failure the init container needs. The probe reads the mounted `auth` from `config.json`, follows the registry's token challenge, and reads the tag's manifest:

| Answer | Marker | Result |
|---|---|---|
| the manifest is there | `tag_exists` | the init container fails, so the build never runs |
| 401 or 403 | `registry_denied` | refused |
| 404 whose body says `manifest unknown` | (silent) | the clone and the build run |
| anything else | `registry_error` | refused, rather than guessing |

`build show` reports the marker as the refusal's code. An existing tag is never overwritten: a DGD may already pull it. The first real build confirms the probe's registry flow (`references/bot.md`, section 8 of the runbook).

**The git key and the pinned host key.** `secret git --repo <owner>/<repo>` names the ref by convention, `op://Fleet/<owner>-<repo> deploy key/private key`, fetches it with `fleet secret get` itself (no value passes through a caller, argv, a file or a pipe from outside), and creates:

- Secret `git` with key `id_ed25519` (the 1Password field is `private key`; a Secret key cannot contain a space);
- ConfigMap `git-known-hosts` with key `known_hosts`, holding GitHub's published ed25519 host key.

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
- the tag-check probe's registry flow on `nvcr.io`: the token challenge in the form the probe expects, and `wget` over TLS in the pinned builder image (Wolfi busybox, whose `wget` runs the `ssl_client` applet). A 404 for a missing tag contains `manifest unknown`;
- the Crane image at `v0.20.3` by digest: `v0.20.2`, named by the plan, is no longer served by `gcr.io/go-containerregistry/crane`;
- every container's `ephemeral-storage` request and limit are accepted together with the node's allocatable storage;
- the bot may `create jobs.batch` in the pre-made namespace.

## Tests

```bash
timeout 300 python3 test/test-k8s-guard.py </dev/null
```
