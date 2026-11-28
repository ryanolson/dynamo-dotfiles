---
name: k8s-vcluster
description: Work inside the owner's standing vClusters on the shared Kubernetes GPU clusters as the agents' bot. Connect with the bot's kubeconfig and a port-forward, then apply CRDs, operators, RBAC and namespaces inside the vCluster without touching the host. Use when a task needs cluster-scoped objects (a CRD, an operator, a controller) on a shared cluster.
---

# k8s-vcluster

A vCluster is a small Kubernetes control plane that runs as one pod in one namespace of a host cluster. Inside it the bot is cluster admin: it can create CRDs, operators, ClusterRoles and namespaces. The host sees only ordinary pods in that one namespace. Pods created inside run on the host's real nodes, under the host's policies and GPU reaper.

Read the `k8s-clusters` skill first. Its access rules hold here too. The Linear runbook "Runbook: build and run on our Dynamo k8s clusters", section "Standing vClusters", lists the clusters that have one, what each supports, and the rules. When the runbook and this skill disagree, the runbook wins.

## Where they are

The owner creates one standing vCluster per cluster, with the owner's own login, and keeps it. The bot never creates, upgrades or deletes a vCluster, and never changes the host's cluster-scoped grants for one.

From the profiles (`~/.config/k8s-clusters/profiles.toml`, see `k8s-clusters/references/profiles.md`):

| Value | From the profile |
|---|---|
| host context | `[clusters.<name>] context` |
| host namespace | `[bot] namespace_prefix` + `vcluster` |
| vCluster name | `[bot] developer` |
| kubeconfig Secret | `vc-<vCluster name>`, key `config` |
| API Service | `<vCluster name>`, port 443 |

Only the clusters the runbook's table lists have one. On any other cluster the namespace does not exist; stop.

## Connect

The bot is namespace admin in the host namespace, so it can read the vCluster's kubeconfig Secret and port-forward to its API. No `vcluster` CLI is needed. The commands are in `references/connect.md`. In short:

1. With the bot's kubeconfig and `--context`, decode the Secret's `config` into a file with mode 0600. It is a cluster-admin credential for the vCluster: never print it, log it, or copy it off the host.
2. `kubectl port-forward svc/<vCluster name> :443` in the host namespace, in the background. Read the local port from its first line. Never use a fixed port.
3. Run every vCluster command as `kubectl --kubeconfig <file> --server https://localhost:<port> ...`. Use `localhost`, not `127.0.0.1`: the server certificate names `localhost`.
4. When done, kill the port-forward and delete the file.

The kubeconfig in every vCluster's Secret uses the same context name, so keep one file per cluster and never merge them into one kubeconfig.

## Apply inside the vCluster

Inside, plain `kubectl apply`, `helm` and an operator's own installer work as cluster admin. `k8s-guard` is not used inside a vCluster: its fences and ledger cover the host namespace only.

- Make a namespace per task inside the vCluster, named for the task, and delete it at the end.
- CRDs and other cluster-scoped objects you create stay inside the vCluster. Delete the ones you made when the task ends, unless the runbook says they are shared.
- Pods sync to the host namespace as `<pod>-x-<namespace>-x-<vCluster name>`. `kubectl logs` and `kubectl exec` work through the vCluster's kubeconfig.
- Record each task's objects in the thread's ledger, as the runbook asks for host writes.

## Run a DGD

A DynamoGraphDeployment runs inside a vCluster that has the Dynamo platform in `dynamo-system` (the runbook's table says which ones do; `references/limits.md` says how to install it). Each deployment gets its own namespace inside the vCluster:

1. `vk create namespace <task>`, named for the task.
2. Model weights: see `references/limits.md`, "The host model cache is not a PVC inside". Put the volume in the DGD's pod template.
3. `vk -n <task> apply -f dgd.yaml`. The operator creates a Deployment and Service per component; the pods sync to the host as `<pod>-x-<task>-x-<vCluster name>`.
4. Query the frontend with `vk -n <task> port-forward svc/<dgd>-frontend :8000`.
5. Tear down with `vk delete namespace <task> --wait=true`. Then, with the bot's kubeconfig, check the host namespace: no pod or PVC whose name contains `-x-<task>-x-` remains, and no pod there requests a GPU.

A frontend-only DGD, with no GPU, is the check that the operator works: it reached Ready in about a minute.

## Rules

- **No GPU pods** until the runbook's "Standing vClusters" section says GPU use is open. Until then a GPU pod needs the owner's go and a manual spend record on the issue: who ran it, how many GPUs, how long, and when the GPUs were released.
- Never create workloads in the host namespace directly to get around a rule inside the vCluster.
- Never change anything on the host outside the vCluster's own namespace. Host reads follow the `k8s-clusters` skill.
- Never run the `vcluster` CLI's `create`, `delete`, `upgrade` or `platform` commands.
- Never use the owner's `tsh` login, `~/.tsh` or `~/.kube/config`. The vCluster's credential comes only from its Secret, read with the bot's kubeconfig.

## What is not there

`references/limits.md` lists what a standing vCluster does not provide today, and the workarounds: the host's Dynamo operator (install one inside), the host's model cache PVC (mount NFS inline, or seed a PVC), MNNVL device classes (none without a license), and batch scheduler queues. Read it before planning a workload.
