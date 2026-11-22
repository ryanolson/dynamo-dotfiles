---
name: k8s-clusters
description: Reach the team's shared Kubernetes GPU clusters as the agents' bot, and check access read only with k8s-check. Use before any command against one of those clusters, and to pick a cluster.
---

# k8s-clusters

The source of truth is the Linear document "Runbook: build and run on our Dynamo k8s clusters" on the WCEP initiative. Read it before any command against a cluster. It says which clusters we may use, which need the owner's word, and how to build, deploy, check and tear down. This skill adds only the commands. When the runbook and this skill disagree, the runbook wins.

## Rules the commands enforce

- Use the agents' Teleport bot only: never the owner's `tsh` login, `~/.tsh`, or `~/.kube/config`.
- Pass `--context` on every `kubectl` call. Never rely on the current context.
- Make no write with plain `kubectl`. Writes go through `scripts/k8s-guard` only; its commands and rules are in `references/k8s-guard.md`.
- Astra changes go through `scripts/astra-guard` only; its commands and rules are in `references/astra-guard.md`.

## The bot

How the bot works, how to set up a host, what it can and cannot do, the pre-made namespace, the key rules, and what to do when it fails are in `references/bot.md`. Read it before you set up a host or deploy.

One known gap, tested: the bot cannot create namespaces, so `k8s-guard ns create` refuses `namespace_create_denied` and names `ns claim`, which claims the pre-made namespace with a lease. Claims stay off (`enforcement_unhealthy`) until the admission webhook that counts controller writes exists. `kubectl auth whoami` is not served on these clusters, so `k8s-check` names the bot from its client certificate instead (the report says `"source": "certificate"`). To check access by hand, use `kubectl --context <ctx> get nodes`.

## Find stale deployments and GPU holds

```bash
export KUBECONFIG=<bot.kubeconfig from the profiles>
~/.agents/skills/k8s-clusters/scripts/k8s-stale --text --gpus-only      # every profile that is not `never`
~/.agents/skills/k8s-clusters/scripts/k8s-stale <cluster> --hold-days 1 # one cluster, tighter threshold
```

It reads pods, DynamoGraphDeployments and namespaces on each cluster with the bot's kubeconfig and the pinned kubectl, and names the bot as `k8s-check` does. It changes nothing and contacts no one. It lists candidates for release, most GPU-days first, each with cluster, namespace, owner, GPUs and the reason:

- `gpu_hold_old`: a namespace holds GPUs and its oldest pod is older than `--hold-days` (3).
- `reaper_opt_out`, `reaper_opt_out_expired`: running pods opted out of the GPU reaper, and an opt-out whose expiry has passed.
- `gpu_pod_not_ready`: a running pod holds GPUs and has not been Ready for `--unready-hours` (1).
- `dgd_stuck`: a deployment is not in a done state after `--stuck-hours` (2).
- `dgd_failed`: a deployment is in the failed state. Most hold no GPU; they are clutter.
- `dgd_old`: a deployment older than `--dgd-days` (14) whose running pods hold GPUs.

The API shows no GPU utilization, so a finding means "could be released", not "is idle". Only running pods count as holding GPUs. One hold can match several rules, so do not add GPU counts across findings. Ask the owner; never delete or scale someone else's work. The rules are in `scripts/k8s_stale_rules.py`.

## Check leases against the clusters, and the expiry backstop

```bash
export KUBECONFIG=<bot.kubeconfig from the profiles>
~/.agents/skills/k8s-clusters/scripts/k8s-sweep --text        # read only: every profile that is not `never`
```

It lists our namespaces with their age, thread, expiry, GPUs, pod age and reaper warnings. It matches them against `bb k8s leases` and flags `gpus_no_lease`, `gpus_over_lease`, `gpu_pod_no_dgd` and `lease_namespace_gone`. It also lists ledger entries on the host that no `down` closed. Each run writes the table the steward's digest reads to `~/.local/state/k8s-sweep/digest.md`. `--enforce` is the backstop for when bb is down: after a namespace's expiry it runs `k8s-guard expire`, and one hour later `k8s-guard down`, with the UID the ledger recorded. It never writes with kubectl. Two user timers run the report every hour and `--enforce` every 15 minutes, on hosts with the bot. The rules, the flags and the units are in `references/k8s-sweep.md`.

## Threat model

`k8s-check` and `k8s-guard` protect against accidental use of credentials other than the bot's: a stray kubeconfig, a kuberc, `KUBECONFIG`, a `tsh` exec plugin, a wrong path, or a file swapped by mistake. They do not protect against a malicious process running as the same Unix user, which could replace `~/.local/bin/tbot` or trace the checker. A wrapper compiled into an allowlisted path counts as replacing tbot or kubectl: out of scope, though the kubectl sha256 pin catches one installed after the pin was taken. The bot's own permissions on the server are the boundary against that.

## Profiles

`~/.config/k8s-clusters/profiles.toml` (or `$K8S_CLUSTERS_PROFILES`) holds one profile per cluster: its context, GPUs, taint, storage, pull secret, whether the bot can write, and whether we may use it. It is written by hand from the runbook's section 3, and never committed, because it holds internal names. The schema is in `references/profiles.md`, with a made-up example in `references/profiles.example.toml`.

```bash
~/.agents/skills/k8s-clusters/scripts/k8s-check --list      # validate the profiles, print them as JSON
```

## Read-only access check

```bash
export KUBECONFIG=<bot.kubeconfig from the profiles>
~/.agents/skills/k8s-clusters/scripts/k8s-check <cluster> [--want-crd v1beta1]
```

It runs the runbook's section 5 and prints JSON: the identity (from `kubectl auth whoami`; on a cluster that does not serve it, from the common name of the client certificate in the sealed kubeconfig, and any other `whoami` error still fails), `can-i` inside and outside our namespaces, two probes on a namespace name under our prefix (patch the namespace object; create a ConfigMap in it), the DGD CRD versions served, the operator images, the GPU nodes with their taints and free GPUs, our namespaces, and whether the GPU reaper runs.

It exits 1, with a `failures` list of stable codes, when:

- the cluster has no profile (`unknown_cluster`), or its profile says `never` (`cluster_never`);
- `KUBECONFIG` is unset, lists two files, or is not the bot's file (`kubeconfig_not_bot`);
- the bot kubeconfig has no context for the cluster (`context_missing`);
- that context's user holds anything but tbot's exec plugin or a client certificate: a token, an auth provider, an impersonation, or another command such as `tsh` (`kubeconfig_credentials`);
- the identity is not the bot (`not_bot`); the check stops at once;
- the bot can create DGDs outside our namespaces (`can_i_outside`);
- `--want-crd` names a version the CRD does not serve (`crd_version_not_served`);
- a `kubectl` read fails (`kubectl_error`).

The first five make no cluster call. The bot kubeconfig is read once into a sealed in-memory copy, and the pinned kubectl reads it locally (`kubectl config view --raw --flatten`). From that view a fresh kubeconfig is rendered for the one context, with only the fields tbot v18.7.6 writes: an https server without userinfo, its TLS server name and CA data, and the user's embedded client certificate or tbot's exact exec plugin (an absolute path to the same file as an installed tbot, `kube credentials --destination-dir=<the kubeconfig's directory>`, no env). Any other field is refused by name. An exec plugin is not kept: the check runs that tbot once itself, with a destination it builds (no URI characters), and embeds the client cert and key from tbot's ExecCredential, so the credential `whoami` checks is the one every later call uses. A credential that expires within 5 minutes is taken once more, then refused (`credentials_expired`). Every cluster call reads that rendered, sealed copy through `/proc/self/fd`, so the check needs Linux. kubectl runs only from `/usr/local/bin`, `/usr/bin` or `~/.local/bin`, must be an ELF binary, and must match `[bot] kubectl_sha256` (`kubectl_mismatch`); never a script found on PATH. It runs with kuberc off, and without `KUBECONFIG` or any other `KUBECTL_*` variable.

Every run contacts the cluster. Record it in the thread's ledger, and report it to whoever asked for it.
