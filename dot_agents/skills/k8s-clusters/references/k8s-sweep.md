# k8s-sweep

`k8s-sweep` checks that the `k8s-leases` leases match what the clusters actually run. It reads every cluster in the profiles, reports our namespaces and what they hold, and flags GPU work that no lease covers, which means the guard was bypassed. With `--enforce` it is also the expiry backstop for when bb or the plugin is down. That is its only write, and it makes it through `k8s-guard`.

```bash
export KUBECONFIG=<bot.kubeconfig from the profiles>
~/.agents/skills/k8s-clusters/scripts/k8s-sweep --text            # every profile that is not `never`
~/.agents/skills/k8s-clusters/scripts/k8s-sweep <cluster>          # one cluster, JSON
~/.agents/skills/k8s-clusters/scripts/k8s-sweep --enforce          # the backstop; the timer runs it
```

## What it reads

For each profile that is not `never`, with the bot's kubeconfig and the pinned kubectl, as `k8s-check` does:

- the bot's identity (`auth whoami`, or the client certificate where that is not served). Any other identity stops that cluster;
- our namespaces: `get namespaces -l dynamo.nvidia.com/developer=<[bot] developer>`, kept only when the name starts with `[bot] namespace_prefix`. A namespace without our label, or a person's namespace outside the bot's prefix, is ignored;
- in each one, by `-n` and never with `-A`: its pods, its DGDs, and the lease record `wcep-agent-lease`. The pre-made namespace has no record and is not asked for one;
- each namespace that a live lease or an open ledger entry names but the list did not show, read by name.

It also reads `bb k8s leases --all --json`, the host ledger (`${XDG_STATE_HOME:-~/.local/state}/k8s-guard/ledger.jsonl`), and every thread ledger on the host (`~/.bb-machines/*/thread-storage/*/k8s-ledger.jsonl`, or each `--ledger-glob`).

For each namespace it reports the age, `wcep.agent/thread`, the expiry, the GPUs, the age of the oldest pod, and how many pods carry `gpu-reaper.nvidia.com/state: warned` (as a label or an annotation).

- **GPUs** are the guard's allocation (`k8s_guard_rules.allocation`): for each DGD, the larger of its spec and its live pods, plus GPU pods that no DGD owns. Terminating pods count; finished pods do not.
- **The expiry** is the guard's (`NsView.expires_at`): the record's `expires_at` when the record's `uid` is this namespace's, otherwise the creation annotation `wcep.agent/expires-at`. The pre-made namespace has none.

## Flags

| Flag | Meaning |
|---|---|
| `gpus_no_lease` | The namespace holds GPUs and no `active` lease matches its cluster, name and UID. A `revoking` or `releasing` lease does not cover it; the detail names that lease. |
| `gpus_over_lease` | It holds more GPUs than its active lease grants. |
| `gpu_pod_no_dgd` | A GPU pod whose `nvidia.com/dynamo-graph-deployment-name` label is missing or names a DGD that is not in the namespace. |
| `lease_namespace_gone` | A live lease whose namespace is not found, or has another UID (it was re-created). The plugin should release it. |
| `count_failed` | A GPU request in a form the guard cannot count. |
| `namespace_recreated` | The ledger or the lease recorded another UID for this name. `--enforce` never touches it. |
| `uid_unknown` | The namespace is past its expiry, but no ledger line on this host and no live lease records its UID. `--enforce` leaves it. |
| `expiry_unreadable` | The expiry is not an RFC 3339 time with a time zone. |
| `read_failed` | The namespace's pods, DGDs or lease record could not be read. Its row has no counts, and `--enforce` leaves it, because a lease record it did not read could hold a later expiry. The other namespaces on the cluster go on, and the run exits 1. |

If bb cannot be read, the run reports `leases_unavailable` and raises no lease flag: a list it could not read is not an empty list. With `--json`, an unreachable bb prints its error envelope on stdout, so the sweep accepts only a JSON list. A stale-lease flag is raised only for a cluster that was read, and only for a namespace whose read by name succeeded.

**Open ledger entries** use the guard's own rule (`Guard.ledger_namespaces`). A `Namespace/<ns>` line `up` with `created` opens an entry. A `down` with `deleted` for the same UID closes it. `expire` also writes `down`, but without `deleted`, so it closes nothing. A `Claim/<ns>` line `claim` opens an entry for its lease, and a `Claim/<ns>` `down` for that lease closes it. An entry whose lease the plugin reports `released` or `torn-down` is closed too, because its teardown may have been recorded on another host. `--all` lists only the latest 200 leases, so an entry from an older lease can show as open again. A host keeps each line in its own ledger and in the thread's, so identical lines count once, with both sources named.

## --enforce

For each namespace that is ours, on a profile with `bot_can_write = true`:

| Now | Action |
|---|---|
| before the expiry | nothing; the guard is not called |
| from the expiry, for one hour (the grace period) | `k8s-guard --context <ctx> expire <ns> --uid <uid>`, on every run |
| from the expiry plus one hour | `k8s-guard --context <ctx> down <ns> --uid <uid>` |

- **The UID is a record older than this read.** It is the UID from the newest `ns create` line in the ledgers, or else the UID of the one live lease on that name. It never comes from the live read. A namespace whose live UID differs is flagged `namespace_recreated` and left alone. With no record, it is flagged `uid_unknown` and left alone. The guard refuses a `--uid` that differs from the namespace, and its namespace delete carries the UID as a precondition. So a namespace re-created between the read and the delete is never deleted either (`uid_mismatch`).
- **Never the pre-made namespace, never a namespace already being deleted.** The pre-made namespace keeps no expiry, and only `k8s-leases` ends a claim. A Terminating namespace would only get another `down` line every 15 minutes.
- **The guard decides its own authority**, by the rules in `k8s-guard.md` ("Teardown authority"). With the plugin unreachable, it allows teardown once the namespace's expiry has passed and the `--uid` matches. With the plugin up, it allows teardown only for a lease that is `revoking` or `releasing`, and refuses `lease_active` for an active one. The sweep reports each refusal and acts on none.
- **The guard never runs as a thread.** The sweep removes `BB_THREAD_ID` and `BB_THREAD_STORAGE` from the guard's environment. Run by hand inside the namespace's own thread, `down` would otherwise take the owner's path and release the lease.
- **It writes nothing with kubectl.** Its own kubectl calls are `get` and `auth whoami` only, which `Reader` enforces before it runs anything. Every write comes from the guard.
- **Each call is recorded.** The guard writes its own `down` line to the host ledger. After it, the sweep appends one line with `object: Sweep/<ns>`, `direction: enforce`, `action`, `result` (`ok` or the guard's code), `uid`, `expires_at` and `by: k8s-sweep`. That direction is one the guard's ledger reader ignores.
- On our clusters today, the bot can use only the pre-made namespace. So the backstop has nothing to act on until the bot can create namespaces.

Without `--enforce`, each row still shows what the backstop would do (`not run: report only`).

## Output

Each run replaces `${XDG_STATE_HOME:-~/.local/state}/k8s-sweep/report.json` and `digest.md`. It writes each one to a temporary file and renames it, so a reader never sees half a file. `digest.md` is the table the steward's digest reads. It is one Markdown table, with the host, the time and the mode above it and any errors below it:

| Kind | Cluster | Namespace | Thread | GPUs | Lease | Age | Expires | Flags | Action |
|---|---|---|---|---|---|---|---|---|---|
| `namespace` | profile | name | `wcep.agent/thread` | allocation, `?` if uncounted | `<id> <state> <gpus>`, `-` none, `?` bb unread | namespace age | expiry or `claimed` | `kind: detail; ...` | `expire: ok`, `down: lease_active`, ... |
| `lease` | | its namespace | its thread | its GPUs | the stale lease | | | `lease_namespace_gone: ...` | |
| `ledger` | | | the thread | | its lease | `since <time>` | | `open_namespace` or `open_claim`, the ledgers, and whether it still exists | |

Each host writes its own table. A stale `checked_at` means that host's timer has stopped.

Exit status: 0 when the run completes, since flags are not failures. 1 when a cluster, the lease list or a ledger could not be read, or when an enforcement call failed (the guard printed no JSON, timed out, or failed with `internal_error` or `kubectl_error`). 2 for a usage error.

## Timers

Two systemd user units, deployed only on a host with the `k8s_tbot` block, like `tbot-k8s-agent.service`. Chezmoi never enables or starts them:

- `k8s-sweep.service` and `k8s-sweep.timer`: the report, every hour (`OnCalendar=hourly`);
- `k8s-sweep-enforce.service` and `k8s-sweep-enforce.timer`: `--enforce`, every 15 minutes (`OnCalendar=*:0/15`). With bb down, GPUs stop within 15 minutes of expiry.

Both are `Type=oneshot` with `TimeoutStartSec=30min`, since one `down` may wait 300 seconds for GPU pods. A tick that comes while a run is still going is skipped. Both set `KUBECONFIG` to the bot's kubeconfig and put `~/.local/bin` (the bb wrapper) first on `PATH`. The sweep reads the bb server's URL from `~/.config/bb/server-url` when `BB_SERVER_URL` is unset, as `~/.profile` does. Install on a host with the bot (the owner's step):

```bash
loginctl enable-linger $USER          # timers run without a login session
systemctl --user daemon-reload
systemctl --user enable --now k8s-sweep.timer k8s-sweep-enforce.timer
```

## Tests

```bash
timeout 300 python3 test/test-k8s-sweep.py </dev/null
```

The tests run the rules on made-up JSON, and run the script against the fakes of `test-k8s-guard.py`, with the real guard for `--enforce`. They render the units with chezmoi. Run the guard's suite too after a change to the shared fakes. Use the system `python3`: the profile renderer needs `os.memfd_create`, which uv's managed Python 3.14 lacks.

## Not proven without a cluster

- `kubectl get namespaces -l <label>` and the reads by `-n`, through the bot, on each cluster;
- the guard's `expire` and `down`, run by the sweep from a timer with bb down, against a real namespace: there is none to run them on while the bot can use only the pre-made namespace;
- the timers on the hosts, and the steward's digest reading `digest.md`.
