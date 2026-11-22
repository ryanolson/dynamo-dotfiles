# The kind fixture's cases

`test/kind-fence.sh` runs the two fence policies on Kyverno chart 3.5.2 (v1.15.2) in a throwaway kind cluster, on the dev host only. Every probe goes through the guard's own probe Pods and verdict (`scripts/k8s_fence_rules.py`). Each `check` line prints PASS or FAIL. `PROBE`, `TEXT`, `TALLY`, `EVENTS`, `REPORTS` and `OUTAGE` lines record what the engine answered; the guard's tests pin those texts.

| Case | What it proves |
|---|---|
| Install: both entries, exactly our expression, `failurePolicy: Fail`; the shared entry gains nothing; no entry of ours matches DELETE | The ops read before and after install (F2(b)); scope is by name |
| Audit: the canary refused by its own entry; the mode Pod and a real unlabelled Pod admitted with the warning; not armed, cause "Audit" | The split keeps an audit window; one policy with an enforcing canary rule had none |
| Probes in another namespace and in an unlabelled prefix namespace | No entry of ours sees them |
| Tally: counter and Events per dry-run probe and per real would-be denial; the would-be denial's Event in the namespace; PolicyReport results | T4 conditions 1 and 6; what the recorder reads |
| Enforce: armed | Both entries refuse, the main one with the Pod rule |
| Gate: missing, open, other lease, no controller owner, closing, the late open, `"07"` vs `"7"`, lower seq, same seq other lease, another user, higher seq | The gate read end to end on a real engine (T4 condition 2) and the seq rule |
| Gate delete: not denied; a deleted gate denies with `Gate: missing` | Kyverno 1.15.2 never denies a DELETE on a policy's own webhook; the failure is safe |
| Gate unreadable: the admission controller's ConfigMap read revoked | `Gate: unreadable`, a different message from missing |
| Outage: node cordoned, pod deleted, replicas 1, 110 s away | Both entries stay; `engine_unavailable`, armed; other namespaces and deletes untouched; then the watchdog's emptied window, polled every second to 90 s past ready |
| Scaled to 0, then back | Kyverno deletes every webhook; not armed, the cause names the 0 replicas |
| Main policy removed; canary policy removed | Not armed either way, and the other entry still answers: both are load-bearing (the steward's negative case) |
| Main policy without matchConditions | It joins the shared entry: scope `shared`, not armed (F2(a)) |
| Namespace delete under Enforce | Completes: `has(request.name)` (T4 condition 3, reproduced without it) |

Run: `timeout 2400 bash test/kind-fence.sh </dev/null`. `KIND_FENCE_KEEP=1` keeps the cluster; `KIND_FENCE_WORK` names the work directory, which keeps the log, the webhook reads, the Events and the outage timeline.
