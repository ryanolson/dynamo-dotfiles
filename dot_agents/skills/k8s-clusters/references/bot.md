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

The bot never installs anything cluster-scoped. Everything that writes goes through `scripts/k8s-guard` (see `k8s-guard.md`).

## The namespace

The bot cannot create namespaces, so the owner pre-makes one per writable cluster, with a fixed name that starts with the profile's `namespace_prefix`. It carries two labels:

- `dynamo.nvidia.com/developer: <developer>`
- `dynamo.nvidia.com/developer-agent-rbac: "true"`

A cluster policy then gives the bot admin rights and DynamoGraphDeployment rights inside it. To find it, read only:

```bash
kubectl --context <ctx> get ns -l dynamo.nvidia.com/developer=<developer>
kubectl --context <ctx> -n <ns> get rolebindings      # expect two bindings
```

Rules while the namespace is shared by all tasks:

- One task at a time per cluster. Take the lease first; the lease is the claim.
- Teardown removes the objects your task made. It never deletes the namespace, and nothing may delete it: the bot cannot make a new one.
- Do not touch objects you did not make.

Known gap: `k8s-guard ns create` assumes the bot can create a namespace, so it fails with this bot. Until the guard can use a pre-made namespace, do not write through a different path. Stop and tell the owner.

## The key

- The key is made once with `tbot keypair create`. Only its public half goes to the Teleport configuration.
- The owner keeps one copy in the fleet vault so every host can use the same bot. A host takes the key from the vault into its own storage directory. Pipe it; never print it, log it, or put it on a command line.
- Each host has its own `storage` directory with its own `tbot`. Never copy a storage directory to another host, and never run two `tbot` on one storage. A failed renewal raced by a second identity locked a colleague's bot, and the Teleport team had to reset it.
- Never run `tbot keypair create --overwrite` in a working storage directory.
- tbot rewrites `id_bkp` and its state on each renewal, and `bkp_key_history.json` records past keys. The server can rotate the key, so check a vault copy's public key against the registered one before trusting it.
- If a second host fails to join, stop. Do not retry more than once. A lockout also takes the first host down.

## Images

Images go to the owner's NGC organisation on `nvcr.io`, tagged with the source commit, never `latest`. Log in with a throwaway `DOCKER_CONFIG` fed by `fleet secret get`, and delete it after the push. Pull with a pull secret made by `k8s-guard secret ngc`. The runbook's section "Images" has the steps and the open items.

## When something fails

| Symptom | Likely cause |
| -- | -- |
| `Could not complete bound keypair joining` | No `id_bkp` in the storage directory, or the token does not match the key |
| `selfsubjectreviews API is not enabled` | Expected from `kubectl auth whoami`. `k8s-check` handles it; by hand, use `get nodes` or `auth can-i` |
| `forbidden ... at the cluster scope` | You asked for something cluster-scoped. The bot cannot |
| `denied by Teleport` on a write | The namespace is not one of ours, or lacks the two labels |
| Contexts include a production cluster | `selector_labels` is too wide. Stop `tbot` and fix the block |
