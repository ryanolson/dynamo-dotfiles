# The agents' Teleport bot

How an agent reaches the shared Kubernetes clusters. Read it with the runbook in Linear; the runbook wins where they differ. This file holds only what was tested. The repository is public, so names are placeholders: `<bot>`, `<cluster>`, `<ns>`. The real values live in the host's uncommitted `k8s_tbot` block and in `~/.config/k8s-clusters/profiles.toml`.

## How it works

```
tbot (user service)  --joins with the bot key-->  Teleport proxy
   |  renews a 1-hour certificate every 20 minutes
   v
bot kubeconfig  --kubectl --context <cluster>-->  the cluster
```

- The bot is its own Teleport identity. It is not the owner's `tsh` login, which is `cluster-admin` on these clusters. Never use `tsh`, `~/.tsh`, or `~/.kube/config` from an agent.
- `tbot` keeps one kubeconfig fresh. Its path is `~/.local/share/tbot/<name>/kube/kubeconfig.yaml`, where `<name>` is `k8s_tbot.name`.
- The kubeconfig lists only the clusters the selector labels match. Check that the labels name an environment, or the bot also sees production: its read-only role covers every environment.

## Set up one host

1. Install `tbot` at the version the other hosts run. Check its SHA-256 against the published file.
2. Put the bot key in `~/.local/share/tbot/<name>/storage/id_bkp`, mode 600, in a mode 700 directory. See "The key" below.
3. Add the `k8s_tbot` block to `~/.config/chezmoi/chezmoi.yaml` (see the repository README: "Kubernetes bot for agents"). Set `selector_labels` to include the environment.
4. `chezmoi diff` and then `chezmoi apply` for only `~/.config/tbot` and the `tbot-k8s-agent.service` unit.
5. `systemctl --user start tbot-k8s-agent.service`, then read `journalctl --user -u tbot-k8s-agent.service`. A good start says `Fetched new bot identity` and `Generated identity for Kubernetes access` with a cluster count.
6. Run `systemctl --user enable` on the unit if the host should keep the bot across reboots. Chezmoi never enables it.
7. The same block deploys the `k8s-sweep` units: an hourly read-only report and the 15-minute `--enforce` backstop. Enable them with `systemctl --user enable --now k8s-sweep.timer k8s-sweep-enforce.timer`, with linger on (`references/k8s-sweep.md`, "Timers").

## Each session

```bash
export KUBECONFIG="$HOME/.local/share/tbot/<name>/kube/kubeconfig.yaml"
kubectl config get-contexts -o name          # every context should be a dev cluster
kubectl --context <ctx> get nodes            # a read that proves access
```

Always pass `--context`, and `-n` for namespaced calls. Reads work on every context the kubeconfig lists.

## What the bot can and cannot do

Tested on the first-choice clusters:

| Action | Result |
| -- | -- |
| Read nodes, namespaces, CRDs, pods | Allowed |
| Create a Deployment or a DynamoGraphDeployment in the pre-made namespace | Allowed |
| Create anything in `default` or any other namespace | Denied |
| Create a Namespace | Denied: `cannot create resource "namespaces" ... at the cluster scope` |
| `kubectl auth whoami` | Fails: the cluster does not serve the SelfSubjectReview API. `k8s-check` reads the name from the client certificate instead |
| Write on clusters marked read only in the profile | Denied |

Write only to authorized resources in the pre-made namespace. Follow the runbook for the cluster and workload.

## The namespace

The bot cannot create namespaces, so the owner pre-makes one per writable cluster, with a fixed name that starts with the profile's `namespace_prefix`. It carries two labels:

- `dynamo.nvidia.com/developer: <developer>`
- `dynamo.nvidia.com/developer-agent-rbac: "true"`

A cluster policy then gives the bot admin rights and DynamoGraphDeployment rights inside it. To find it, read only:

```bash
kubectl --context <ctx> get ns -l dynamo.nvidia.com/developer=<developer>
kubectl --context <ctx> -n <ns> get rolebindings      # expect two bindings
```

The pre-made namespace is the write boundary. On a cluster without an enforced fence, use the runbook's lease-free RBAC path. Skip lease commands and submit only Jobs that request zero GPUs.

Coordinate use of the shared namespace. Remove only the objects your task created and leave the namespace in place.

## The key

- The key is made once with `tbot keypair create`. Only its public half goes to the Teleport configuration.
- The owner keeps one copy in the fleet vault so every host can use the same bot. A host takes the key from the vault into its own storage directory. Pipe it; never print it, log it, or put it on a command line.
- Each host has its own `storage` directory with its own `tbot`. Never copy a storage directory to another host, and never run two `tbot` on one storage. A failed renewal raced by a second identity locked a colleague's bot, and the Teleport team had to reset it.
- Never run `tbot keypair create --overwrite` in a working storage directory.
- tbot rewrites `id_bkp` and its state on each renewal, and `bkp_key_history.json` records past keys. The server can rotate the key, so check a vault copy's public key against the registered one before trusting it.
- If a second host fails to join, stop. Do not retry more than once. A lockout also takes the first host down.

## Images

The guard-based image-publishing workflow requires an enforced fence. See the runbook for `k8s-guard build run` in the pre-made namespace. Use native arm64 and amd64 builds. Publish the index with `build index` under the amd64 claim. Tag images with the source commit. Never use `latest`. The runbook's Images section gives the first-run checks, timing records and teardown.

For a private clone, `secret git --repo <owner>/<repo>` reads `op://Fleet/<owner>-<repo> deploy key/private key`. The item holds a read-only key in OpenSSH format. `secret ngc` makes `nvcr-pull` from `op://Fleet/NGC Pull Key/credential`. A missing pull key returns `secret_unavailable`.

The build uses `nvcr-push`. Use `secret ngc-push --exposure-accepted` only after the owner records approval of the PAT exposure. The guard removes the build Secret during claim teardown.

### AKS smoke build

Build x86_64 (`linux/amd64`) natively in `dynamo-aks-dev` because its nodes are x86_64 and the spark-p gate image is arm64.

The steward runs the smoke Job in the pre-made namespace recorded in the measured AKS profile. The operator kubeconfig applies the narrow Role and requests the launcher token. The generated launcher kubeconfig submits the Job using namespace RBAC. The Pod runs as its own unbound ServiceAccount with token mounting disabled.

The smoke is CPU-only. It writes and reads 64 MiB in a native rootless BuildKit step, exports an OCI image to `emptyDir` and logs the digest.

The launcher can expose any Secret in this namespace through Job creation and Pod log reads, within the bot's existing rights.

Use the namespace from that profile for `SMOKE_NAMESPACE`. For example, `agent-namespace` stands in for that name.

Set `C` to the direct AKS context. Set `OPERATOR_KUBECONFIG` to an authorized kubeconfig that can read the namespace CA and request ServiceAccount tokens. The launcher requires the generated `SMOKE_KUBECONFIG` file. Its explicit `--kubeconfig` selects the launcher identity even if `KUBECONFIG` points to Teleport.

Check the namespace's `pod-security.kubernetes.io/enforce` label before submitting. Baseline and Restricted reject the unconfined seccomp and AppArmor profiles the builder uses. [Kubernetes Pod Security Standards](https://kubernetes.io/docs/concepts/security/pod-security-standards/)

```bash
kubectl --kubeconfig "${OPERATOR_KUBECONFIG:?set the namespace operator kubeconfig}" \
  --context "${C:?set the AKS context}" \
  get namespace "${SMOKE_NAMESPACE:?set the pre-made AKS namespace}" --show-labels
```

Apply [rbac.yaml](../smoke/rbac.yaml) from the dotfiles checkout with the authorized operator kubeconfig.

```bash
kubectl --kubeconfig "${OPERATOR_KUBECONFIG:?set the namespace operator kubeconfig}" \
  --context "${C:?set the AKS context}" -n "${SMOKE_NAMESPACE:?set the pre-made AKS namespace}" \
  apply -f dot_agents/skills/k8s-clusters/smoke/rbac.yaml
```

From the dotfiles checkout, set `SMOKE_KUBECONFIG` to a new file path. The launcher authenticates as `gdl-build-smoke` with a TokenRequest bearer token requested for 20 minutes. The generated kubeconfig pins `certificate-authority-data` from `kube-root-ca.crt` and uses mode `0600`.

```bash
(
  set +x
  set -euo pipefail
  : "${OPERATOR_KUBECONFIG:?set the namespace operator kubeconfig}"
  : "${SMOKE_KUBECONFIG:?set the ServiceAccount kubeconfig}"
  namespace="${SMOKE_NAMESPACE:?set the pre-made AKS namespace}"
  umask 077
  set -o noclobber
  operator=(kubectl --kubeconfig "$OPERATOR_KUBECONFIG" --context "$C")
  ca=$("${operator[@]}" -n "$namespace" get configmap kube-root-ca.crt -o json |
    jq -er '.data["ca.crt"] |
      if type == "string" and length > 0 then @base64
      else error("kube-root-ca.crt is missing ca.crt") end')
  cluster=$("${operator[@]}" config view --minify -o json |
    jq -ce --arg ca "$ca" '.clusters[0] |
      select(.name and (.cluster.server | startswith("https://"))) |
      {name, cluster: {server: .cluster.server, "certificate-authority-data": $ca}}')
  config=$("${operator[@]}" -n "$namespace" \
    create token gdl-build-smoke --duration=20m |
    jq -Rsec --argjson cluster "$cluster" --arg namespace "$namespace" '
      sub("[\r\n]+$"; "") |
      if length == 0 then error("missing launcher token") else
        {apiVersion: "v1", kind: "Config",
         clusters: [$cluster], users: [{name: "launcher", user: {token: .}}],
         contexts: [{name: "aks-smoke", context: {
           cluster: $cluster.name, user: "launcher", namespace: $namespace}}],
         "current-context": "aks-smoke"}
      end')
  printf '%s\n' "$config" > "$SMOKE_KUBECONFIG"
)
```

Use the launcher kubeconfig for both admission validation and submission.

```bash
(
  set -eu
  job=dot_agents/skills/k8s-clusters/smoke/job.yaml
  smoke=(kubectl --kubeconfig "${SMOKE_KUBECONFIG:?set the ServiceAccount kubeconfig}" --context aks-smoke -n "${SMOKE_NAMESPACE:?set the pre-made AKS namespace}")
  "${smoke[@]}" create --dry-run=server -f "$job"
  "${smoke[@]}" create -f "$job"
)
```

Poll with the launcher kubeconfig:

```bash
kubectl --kubeconfig "${SMOKE_KUBECONFIG:?set the ServiceAccount kubeconfig}" \
  --context aks-smoke -n "${SMOKE_NAMESPACE:?set the pre-made AKS namespace}" get pods \
  -l batch.kubernetes.io/job-name=gdl-build-smoke
```

Once its container starts, set `pod` to its name and stream its logs. The Job's deadline can delete the Pod and its logs.

```bash
(
  set -o pipefail
  kubectl --kubeconfig "${SMOKE_KUBECONFIG:?set the ServiceAccount kubeconfig}" \
    --context aks-smoke -n "${SMOKE_NAMESPACE:?set the pre-made AKS namespace}" logs --follow "pod/${pod:?set the smoke Pod name}" -c build |
    tee gdl-build-smoke.log
)
```

Check the Job status while the Job object is still present.

```bash
kubectl --kubeconfig "${SMOKE_KUBECONFIG:?set the ServiceAccount kubeconfig}" \
  --context aks-smoke -n "${SMOKE_NAMESPACE:?set the pre-made AKS namespace}" get job gdl-build-smoke -o yaml
```

Require `Complete=True`, `buildkit-smoke-ok`, and `containerimage.digest`. Remove the launcher kubeconfig when done. Keep the smoke ServiceAccounts, Role and RoleBinding installed for later runs.

If the token expires, a new request returns `Unauthorized`. Stop polling and record the smoke run as failed to verify. The Job can continue until its 600-second deadline, with a retry budget of zero.

If the Pod never starts or the Job fails, read the events with the operator kubeconfig. A `FailedCreate` event on the Job can explain why there is no Pod. If there is a Pod, query events with its name too.

```bash
kubectl --kubeconfig "${OPERATOR_KUBECONFIG:?set the namespace operator kubeconfig}" \
  --context "${C:?set the AKS context}" -n "${SMOKE_NAMESPACE:?set the pre-made AKS namespace}" get events \
  --field-selector involvedObject.name=gdl-build-smoke
```

Validate the manifests from the checkout without cluster access:

```bash
timeout 60 kubeconform -strict -summary -kubernetes-version 1.30.0 \
  dot_agents/skills/k8s-clusters/smoke
timeout 60 uv run --no-project --with pyyaml python test/test-k8s-smoke.py
```

## When something fails

| Symptom | Likely cause |
| -- | -- |
| `Could not complete bound keypair joining` | No `id_bkp` in the storage directory, or the token does not match the key |
| `selfsubjectreviews API is not enabled` | Expected from `kubectl auth whoami`. `k8s-check` handles it; by hand, use `get nodes` or `auth can-i` |
| `forbidden ... at the cluster scope` | You asked for something cluster-scoped. The bot cannot |
| `denied by Teleport` on a write | The namespace is not one of ours, or lacks the two labels |
| Contexts include a production cluster | `selector_labels` is too wide. Stop `tbot` and fix the block |
