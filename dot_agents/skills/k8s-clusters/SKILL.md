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

It runs the runbook's section 5 and prints JSON: the identity, `can-i` inside and outside our namespaces, two probes on a namespace name under our prefix (patch the namespace object; create a ConfigMap in it), the DGD CRD versions served, the operator images, the GPU nodes with their taints and free GPUs, our namespaces, and whether the GPU reaper runs.

It exits 1, with a `failures` list of stable codes, when:

- the cluster has no profile (`unknown_cluster`), or its profile says `never` (`cluster_never`);
- `KUBECONFIG` is unset, lists two files, or is not the bot's file (`kubeconfig_not_bot`);
- the bot kubeconfig has no context for the cluster (`context_missing`);
- that context's user holds anything but tbot's exec plugin or a client certificate: a token, an auth provider, an impersonation, or another command such as `tsh` (`kubeconfig_credentials`);
- the identity is not the bot (`not_bot`); the check stops at once;
- the bot can create DGDs outside our namespaces (`can_i_outside`);
- `--want-crd` names a version the CRD does not serve (`crd_version_not_served`);
- a `kubectl` read fails (`kubectl_error`).

The first five make no cluster call; the bot kubeconfig is read locally with `kubectl config view`. Every `kubectl` call reads the one kubeconfig file opened at the start, through `/proc/self/fd`, so the check needs Linux. kubectl runs without `KUBECONFIG`, without any other `KUBECTL_*` variable, and with kuberc off. A `warnings` list reports where the profile and the cluster disagree. Fix the profile from the runbook's sources, not from the warning alone.

Every run contacts the cluster. Record it in the thread's ledger, and report it to whoever asked for it.
