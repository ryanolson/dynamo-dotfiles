#!/usr/bin/env bash
# The lease fence's kind fixture: the two rendered policies on a real Kyverno v1.15.2 (chart 3.5.2) in a throwaway
# kind cluster. It runs the guard's own probe Pods and verdict (scripts/k8s_fence_rules.py) against the real
# engine, and records every server text the guard pins. Run by hand on the dev host only; it needs kind, helm
# and docker. The cases and what each proves are in test/kind-fence-cases.md.
#
#   timeout 1800 bash test/kind-fence.sh </dev/null
#
# Safety: every kubectl call runs inside the kind node container against its own admin.conf, so no host
# kubeconfig, context or credential can be reached. helm gets a kubeconfig written by `kind get kubeconfig` into
# the work directory, and the script refuses to go on unless its server is 127.0.0.1 and its context kind-*.
# The cluster is deleted on exit unless KIND_FENCE_KEEP=1.
set -uo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(dirname "$HERE")
SKILL=$ROOT/dot_agents/skills/k8s-clusters
CLUSTER=${KIND_FENCE_CLUSTER:-agent-fence}
CHART_VERSION=3.5.2
WORK=${KIND_FENCE_WORK:-$(mktemp -d)}
mkdir -p "$WORK"
LOG=$WORK/kind-fence.log
: > "$LOG"
# Made-up names, as in the CLI tests. The fixture's bot is a user the kind admin impersonates.
PREMADE=dyntest-fixture-agent
PREFIX=dyntest-fixture-
OTHER=dyntest-fixture-other
FOREIGN=team-x
BOT=fence-bot
OWNER=fixture-owner
CONTACT=fixture-contact
RUNBOOK="fixture runbook section 10"
LEASE=lse_0123456789abcdef
OUTAGE_SECONDS=${KIND_FENCE_OUTAGE_SECONDS:-110}
FAILS=0

for tool in kind helm docker python3 jq; do
  command -v "$tool" >/dev/null || { echo "kind-fence: $tool is not installed" >&2; exit 2; }
done

say() { printf '%s\n' "$*" | tee -a "$LOG"; }
pass() { say "PASS $1"; }
fail() { say "FAIL $1"; FAILS=$((FAILS + 1)); }
check() { # name, then a command whose exit status decides
  local name=$1; shift
  if "$@"; then pass "$name"; else fail "$name"; fi
}

NODE=$CLUSTER-control-plane
K() { docker exec -i "$NODE" kubectl --kubeconfig=/etc/kubernetes/admin.conf "$@"; }
KB() { K --as="$BOT" "$@"; }

cleanup() {
  if [ -z "${KIND_FENCE_KEEP:-}" ]; then kind delete cluster --name "$CLUSTER" >/dev/null 2>&1; fi
}
trap cleanup EXIT

HELPER=$WORK/helper.py
cat > "$HELPER" <<'PY'
import json, sys
sys.dont_write_bytecode = True
sys.path.insert(0, sys.argv[1])
import k8s_fence_rules as f
cmd = sys.argv[2]
if cmd == 'render':
    text = open(sys.argv[3]).read()
    out = f.render_fence_policy(text, *sys.argv[4:10])
    if len(sys.argv) > 10 and sys.argv[10] == 'no-match-conditions':
        main, canary = out.split('\n---\n')
        out = f._without_mc(main) + '\n---\n' + canary  # only the main policy lost them
    print(out, end='')
elif cmd == 'probe-pod':
    print(json.dumps(f.probe_pod(sys.argv[3], sys.argv[4])))
elif cmd == 'verdict':
    crc, cerr, mrc, merr = int(sys.argv[3]), open(sys.argv[4]).read(), int(sys.argv[5]), open(sys.argv[6]).read()
    print(json.dumps(f.fence_verdict(crc, cerr, mrc, merr)))
elif cmd == 'gate':
    print(json.dumps(f.gate_object(sys.argv[3], sys.argv[4], sys.argv[5], int(sys.argv[6]), 'uid-fixture')))
PY
helper() { python3 -I "$HELPER" "$SKILL/scripts" "$@"; }

render() { # [no-match-conditions]
  helper render "$SKILL/policy/agent-lease-fence.template.yaml" "$PREMADE" "$PREFIX" "$BOT" "$OWNER" "$CONTACT" \
    "$RUNBOOK" "$@"
}

# The probe, exactly as the guard sends it: two server dry-run creates, as the bot. Prints the verdict JSON.
probe() {
  local ns=${1:-$PREMADE} crc mrc
  helper probe-pod canary "$ns" | KB create -f - --dry-run=server -o name >/dev/null 2>"$WORK/canary.err"; crc=$?
  helper probe-pod mode "$ns" | KB create -f - --dry-run=server -o name >/dev/null 2>"$WORK/mode.err"; mrc=$?
  helper verdict "$crc" "$WORK/canary.err" "$mrc" "$WORK/mode.err"
}
is() { # verdict-json jq-expression
  jq -e "$2" <<<"$1" >/dev/null
}

webhooks() { K get validatingwebhookconfiguration kyverno-resource-validating-webhook-cfg -o json 2>/dev/null; }
MAIN_ENTRY=validate.kyverno.svc-fail-finegrained-agent-lease-fence
CANARY_ENTRY=validate.kyverno.svc-fail-finegrained-agent-lease-fence-canary
has_entry() { # [entry] -> it is in the configuration (default the main policy's)
  webhooks | jq -e --arg n "${1:-$MAIN_ENTRY}" '[.webhooks[]?.name] | index($n) != null' >/dev/null; }
both_entries() { has_entry "$MAIN_ENTRY" && has_entry "$CANARY_ENTRY"; }
no_entries() { ! has_entry "$MAIN_ENTRY" && ! has_entry "$CANARY_ENTRY"; }
admission_pod() { K -n kyverno get pods -l app.kubernetes.io/component=admission-controller -o name 2>/dev/null; }
admission_ready() { [ "$(K -n kyverno get pods -l app.kubernetes.io/component=admission-controller \
  -o jsonpath='{.items[*].status.containerStatuses[0].ready}' 2>/dev/null)" = true ]; }
set_canary_absent() { K delete clusterpolicy agent-lease-fence-canary --ignore-not-found >/dev/null; }
wait_for() { # seconds, then a command
  local n=$1; shift
  for _ in $(seq "$n"); do "$@" && return 0; sleep 1; done
  return 1
}

pod_json() { # name lease owner(yes|no) [extra-label]
  local owner=''
  [ "$3" = yes ] && owner=',"ownerReferences":[{"apiVersion":"apps/v1","kind":"ReplicaSet","name":"fixture-rs","uid":"00000000-0000-4000-8000-000000000001","controller":true}]'
  printf '{"apiVersion":"v1","kind":"Pod","metadata":{"name":"%s","namespace":"%s","labels":{"wcep.agent/lease":"%s"}%s},"spec":{"restartPolicy":"Never","containers":[{"name":"main","image":"registry.k8s.io/pause:3.10","resources":{"requests":{"cpu":"1m","memory":"4Mi"},"limits":{"cpu":"1m","memory":"4Mi"}}}]}}' \
    "$1" "$PREMADE" "$2" "$owner"
}
dry_pod() { # name lease owner -> exit status; stderr in $WORK/pod.err
  pod_json "$@" | KB create -f - --dry-run=server -o name >/dev/null 2>"$WORK/pod.err"
}
gate_write() { # verb(create|replace) lease state seq [as]
  local as=${5:-$BOT} body
  body=$(helper gate "$PREMADE" "$2" "$3" "$4")
  if [ "$1" = replace ]; then
    local rv; rv=$(K -n "$PREMADE" get configmap wcep-agent-fence -o jsonpath='{.metadata.resourceVersion}')
    body=$(jq --arg rv "$rv" '.metadata.resourceVersion = $rv' <<<"$body")
  fi
  K --as="$as" -n "$PREMADE" "$1" -f - -o name <<<"$body" >/dev/null 2>"$WORK/gate.err"
}
metrics() { K get --raw /api/v1/namespaces/kyverno/services/kyverno-svc-metrics:8000/proxy/metrics 2>/dev/null \
  | grep '^kyverno_policy_results_total' | grep 'policy_name="agent-lease-fence"'; }
metric_sum() { # rule result -> total over every label set for that rule and result
  metrics | grep "rule_name=\"$1\"" | grep "rule_result=\"$2\"" | awk '{s += $NF} END {print s + 0}'
}
events_of() { K -n "$PREMADE" get events -o json | jq '[.items[] | select((.message // "") | contains("agent-lease-fence"))] | length'; }
set_action() { # Audit | Enforce
  K patch clusterpolicy agent-lease-fence --type=merge -p "{\"spec\":{\"validationFailureAction\":\"$1\"}}" >/dev/null
  sleep 3  # the policy cache picks the change up asynchronously
}
record_text() { # label file
  say "TEXT $1: $(tr '\n' ' ' <"$2" | sed 's/  */ /g')"
}

# --- the cluster -------------------------------------------------------------------------------------------
say "kind-fence $(date -u +%FT%TZ) on $(uname -m); kind $(kind version | awk '{print $2}'); helm $(helm version --short)"
kind delete cluster --name "$CLUSTER" >/dev/null 2>&1
kind create cluster --name "$CLUSTER" --wait 180s >>"$LOG" 2>&1 || { say "FAIL kind create"; exit 1; }
kind get kubeconfig --name "$CLUSTER" > "$WORK/kubeconfig"
server=$(awk '/server:/ {print $2; exit}' "$WORK/kubeconfig")
context=$(awk '/current-context:/ {print $2; exit}' "$WORK/kubeconfig")
case "$server" in https://127.0.0.1:*) ;; *) say "FAIL the kubeconfig names $server, not 127.0.0.1"; exit 1;; esac
case "$context" in kind-*) ;; *) say "FAIL the kubeconfig's context is $context, not kind-*"; exit 1;; esac
say "server $server context $context; node k8s $(K version -o json | jq -r .serverVersion.gitVersion)"

helm pull kyverno --repo https://kyverno.github.io/kyverno --version "$CHART_VERSION" -d "$WORK" >>"$LOG" 2>&1 \
  || { say "FAIL helm pull"; exit 1; }
helm --kubeconfig "$WORK/kubeconfig" --kube-context "$context" install kyverno "$WORK/kyverno-$CHART_VERSION.tgz" \
  -n kyverno --create-namespace --wait --timeout 600s >>"$LOG" 2>&1 || { say "FAIL helm install"; exit 1; }
say "kyverno $(K -n kyverno get deploy kyverno-admission-controller -o jsonpath='{.spec.template.spec.containers[0].image}')"

K create namespace "$PREMADE" >/dev/null
K label namespace "$PREMADE" dynamo.nvidia.com/agent-lease-fence=true >/dev/null
K create namespace "$OTHER" >/dev/null
K create namespace "$FOREIGN" >/dev/null
for ns in "$PREMADE" "$OTHER" "$FOREIGN"; do
  K -n "$ns" create rolebinding fence-bot-admin --clusterrole=admin --user="$BOT" >/dev/null
done

canary_sum() { K get --raw /api/v1/namespaces/kyverno/services/kyverno-svc-metrics:8000/proxy/metrics 2>/dev/null \
  | grep '^kyverno_policy_results_total' | grep 'policy_name="agent-lease-fence-canary"' | grep 'rule_result="fail"' \
  | awk '{s += $NF} END {print s + 0}'; }

# --- install: the ops read before and after (F2(b)) ----------------------------------------------------------
webhooks | jq '[.webhooks[]? | {name, failurePolicy, rules: [.rules[]? | {operations, resources}]}]' > "$WORK/webhooks-before.json"
render > "$WORK/policy.yaml"
K apply -f - <"$WORK/policy.yaml" >/dev/null
check "install: both per-policy entries appear" wait_for 60 both_entries
webhooks | jq '[.webhooks[]? | {name, failurePolicy, matchConditions, rules: [.rules[]? | {operations, resources}]}]' > "$WORK/webhooks-after.json"
for e in "$MAIN_ENTRY" "$CANARY_ENTRY"; do
  entry=$(jq -c --arg n "$e" '.[] | select(.name == $n)' "$WORK/webhooks-after.json")
  say "ENTRY $entry"
  check "install: $e fails closed and carries exactly our expression" \
    jq -e --arg x "request.namespace in ['$PREMADE'] && (request.resource.resource != 'configmaps' || (has(request.name) && request.name == 'wcep-agent-fence'))" \
    '.failurePolicy == "Fail" and (.matchConditions | length == 1) and .matchConditions[0].expression == $x' <<<"$entry"
done
shared_before=$(jq -c '[.[] | select(.name == "validate.kyverno.svc-fail") | .rules[]? | .resources[]?] | unique' "$WORK/webhooks-before.json")
shared_after=$(jq -c '[.[] | select(.name == "validate.kyverno.svc-fail") | .rules[]? | .resources[]?] | unique' "$WORK/webhooks-after.json")
say "SHARED before $shared_before after $shared_after"
check "install: the shared entry gained no resource" test "$shared_before" = "$shared_after"
check "install: no entry of ours matches DELETE" \
  jq -e '[.[] | select(.name | test("agent-lease-fence")) | .rules[]? | .operations[]?] | index("DELETE") == null' "$WORK/webhooks-after.json"

# --- Audit: the canary enforces, the main policy only records ------------------------------------------------
sleep 5
v=$(probe); say "PROBE audit $v"
record_text "canary refusal (Audit)" "$WORK/canary.err"
record_text "mode answer (Audit)" "$WORK/mode.err"
check "audit: the canary is refused by the canary policy's own entry" is "$v" '.canary == "finegrained"'
check "audit: the mode Pod is admitted with the policy's warning" is "$v" '.mode == "audit"'
check "audit: not armed, and the cause says Audit" is "$v" '.armed == false and (.cause | test("Audit"))'
check "probe in another namespace: no entry of ours sees it" is "$(probe "$FOREIGN")" '.canary == "disarmed" and .mode == "absent"'
check "probe in a prefix namespace without the label: no entry of ours sees it" is "$(probe "$OTHER")" '.canary == "disarmed"'

# The audit tally (T4 conditions 1 and 6): one probe's Events and counter increments; whether dry runs count.
sleep 5
c0=$(canary_sum); m0=$(metric_sum pods-need-the-open-lease fail); e0=$(events_of)
for _ in 1 2 3; do probe >/dev/null; done
sleep 8
c1=$(canary_sum); m1=$(metric_sum pods-need-the-open-lease fail); e1=$(events_of)
say "TALLY 3 dry-run probes: probe-canary fail +$((c1 - c0)), pods-need-the-open-lease fail +$((m1 - m0)), events in the namespace +$((e1 - e0))"
say "METRIC $(metrics | head -2 | tr '\n' ' ')"
pod_json audit-would-deny "" no | KB create -f - -o name >/dev/null 2>"$WORK/pod.err"; rc=$?
check "audit: a real would-be denial is admitted" test "$rc" = 0
record_text "would-be denial answer (Audit)" "$WORK/pod.err"
sleep 10
m2=$(metric_sum pods-need-the-open-lease fail); e2=$(events_of)
say "TALLY 1 real would-be denial: pods-need-the-open-lease fail +$((m2 - m1)), events in the namespace +$((e2 - e1))"
check "audit: the real would-be denial moves the counter" test "$((m2 - m1))" -ge 1
K -n "$PREMADE" get events -o json | jq -c '.items[] | select((.message // "") | contains("agent-lease-fence")) | {reason, type, involvedObject: {kind: .involvedObject.kind, name: .involvedObject.name}, reportingComponent, count, message: .message[0:160]}' > "$WORK/events.jsonl"
say "EVENTS $(wc -l <"$WORK/events.jsonl") in the namespace naming the policy; for the real Pod: $(grep -c '"audit-would-deny"' "$WORK/events.jsonl")"
check "audit: the would-be denial has an Event in the namespace (what the recorder reads)" grep -q '"audit-would-deny"' "$WORK/events.jsonl"
sleep 20
K -n "$PREMADE" get policyreports -o json > "$WORK/policyreports.json" 2>/dev/null
say "REPORTS $(jq '[.items[]?.results[]? | select(.policy == "agent-lease-fence")] | length' "$WORK/policyreports.json") results naming the policy"
K -n "$PREMADE" delete pod audit-would-deny --wait=false >/dev/null 2>&1

# --- Enforce: armed, and the gate read end to end (T4 condition 2) -------------------------------------------
set_action Enforce
v=$(probe); say "PROBE enforce $v"
record_text "mode refusal (Enforce)" "$WORK/mode.err"
check "enforce: armed (both entries, and the main one refuses the mode Pod)" \
  is "$v" '.armed == true and .canary == "finegrained" and .mode == "enforcing" and .mode_entry == "finegrained"'

dry_pod gate-missing "$LEASE" yes; rc=$?
record_text "missing gate" "$WORK/pod.err"
check "gate missing: an owned, labelled Pod is denied with Gate: missing" \
  bash -c "test $rc != 0 && grep -q 'Gate: missing' '$WORK/pod.err'"
gate_write create "$LEASE" active 7; rc=$?
check "gate: the bot creates active(L, 7)" test "$rc" = 0
dry_pod gate-open "$LEASE" yes; rc=$?
check "gate open: an owned, labelled Pod is admitted" test "$rc" = 0
dry_pod gate-open-other "lse_fedcba9876543210" yes; rc=$?
check "gate open: another lease's Pod is denied" test "$rc" != 0
dry_pod gate-open-ownerless "$LEASE" no; rc=$?
check "gate open: a Pod with no controller owner is denied" test "$rc" != 0
gate_write replace "$LEASE" closing 7; rc=$?
check "gate: active(7) -> closing(7) is allowed" test "$rc" = 0
dry_pod gate-closing "$LEASE" yes; rc=$?
record_text "closing gate" "$WORK/pod.err"
check "gate closing: the Pod is denied with Gate: closing" bash -c "test $rc != 0 && grep -q 'Gate: closing' '$WORK/pod.err'"
gate_write replace "$LEASE" active 7; rc=$?
record_text "late open" "$WORK/gate.err"
check "gate: closing(7) -> active(7), the late open, is denied" test "$rc" != 0
body=$(helper gate "$PREMADE" "$LEASE" active 7 | jq '.data.seq = "07"')
rv=$(K -n "$PREMADE" get configmap wcep-agent-fence -o jsonpath='{.metadata.resourceVersion}')
jq --arg rv "$rv" '.metadata.resourceVersion = $rv' <<<"$body" | KB -n "$PREMADE" replace -f - -o name >/dev/null 2>"$WORK/gate.err"; rc=$?
check "gate: closing(\"7\") -> active(\"07\") is denied" test "$rc" != 0
gate_write replace "lse_fedcba9876543210" active 6; rc=$?
check "gate: a lower seq is denied" test "$rc" != 0
gate_write replace "lse_fedcba9876543210" closing 7; rc=$?
check "gate: the same seq with another lease is denied" test "$rc" != 0
gate_write replace "$LEASE" active 8 kubernetes-admin; rc=$?
check "gate: a write by another user is denied" test "$rc" != 0
gate_write replace "lse_fedcba9876543210" active 8; rc=$?
check "gate: a higher seq from closing is allowed" test "$rc" = 0
# A DELETE is not the policy's (Kyverno 1.15.2 never denies one on a policy's own webhook): the guard never
# deletes the gate, and a deleted gate reads missing, which denies every Pod.
K -n "$PREMADE" delete configmap wcep-agent-fence >/dev/null 2>&1; rc=$?
check "gate delete: not denied by the policy (measured), so the guard must never send one" test "$rc" = 0
dry_pod gate-deleted "lse_fedcba9876543210" yes; rc=$?
check "gate deleted: an owned Pod of the last lease is denied with Gate: missing (fails safe)" \
  bash -c "test $rc != 0 && grep -q 'Gate: missing' '$WORK/pod.err'"
gate_write create "lse_fedcba9876543210" active 8; rc=$?
check "gate: the bot re-creates it" test "$rc" = 0

# An unreadable gate: take the admission controller's ConfigMap read away, then give it back.
sa=system:serviceaccount:kyverno:kyverno-admission-controller
can_read() { K auth can-i list configmaps -n "$PREMADE" --as="$sa" >/dev/null 2>&1; }
cannot_read() { ! can_read; }
grants=$(K get clusterrolebinding -o json | jq -r '.items[] | select(any(.subjects[]?; .kind == "ServiceAccount" and .name == "kyverno-admission-controller" and .namespace == "kyverno")) | .metadata.name')
K get clusterrolebinding $grants -o json > "$WORK/grants.json"
for g in $grants; do K delete clusterrolebinding "$g" >/dev/null; done
if wait_for 20 cannot_read; then
  sleep 3
  dry_pod gate-unreadable "lse_fedcba9876543210" yes; rc=$?
  record_text "unreadable gate" "$WORK/pod.err"
  check "gate unreadable: the Pod is denied with Gate: unreadable" bash -c "test $rc != 0 && grep -q 'Gate: unreadable' '$WORK/pod.err'"
else
  fail "gate unreadable: could not revoke the admission controller's ConfigMap read"
fi
jq 'del(.items[].metadata.resourceVersion, .items[].metadata.uid, .items[].metadata.creationTimestamp)' "$WORK/grants.json" | K apply -f - >/dev/null
check "gate unreadable: the read is back" wait_for 30 can_read

# --- a real outage (F2(a), F1): the pod gone, replicas still 1 -------------------------------------------------
K -n "$PREMADE" create configmap ordinary --from-literal=a=1 >/dev/null
K cordon "$NODE" >/dev/null
K -n kyverno delete pod -l app.kubernetes.io/component=admission-controller --wait=true >/dev/null
down_at=$(date +%s)
check "outage: both entries stay while the engine is down" both_entries
v=$(probe); say "PROBE engine-down $v"
record_text "canary with the engine down" "$WORK/canary.err"
record_text "mode with the engine down" "$WORK/mode.err"
check "outage: the probe reads engine_unavailable and the fence is armed" \
  is "$v" '.canary == "engine_unavailable" and .mode == "engine_unavailable" and .armed == true'
helper probe-pod mode "$FOREIGN" | KB create -f - --dry-run=server -o name >/dev/null 2>"$WORK/pod.err"; rc=$?
check "outage: a Pod create in another namespace succeeds" test "$rc" = 0
K -n "$PREMADE" create configmap ordinary2 --from-literal=a=1 -o name >/dev/null 2>"$WORK/pod.err"; rc=$?
check "outage: a non-gate ConfigMap write in the enrolled namespace succeeds" test "$rc" = 0
K -n "$PREMADE" delete configmap ordinary -o name >/dev/null 2>"$WORK/pod.err"; rc=$?
check "outage: a delete in the enrolled namespace succeeds" test "$rc" = 0
while [ $(( $(date +%s) - down_at )) -lt "$OUTAGE_SECONDS" ]; do sleep 5; done
K uncordon "$NODE" >/dev/null
up_at=$(date +%s); ready_at=''; : > "$WORK/outage.jsonl"
while :; do
  t=$(( $(date +%s) - up_at ))
  if has_entry; then e=1; else e=0; fi
  if admission_ready; then r=1; [ -z "$ready_at" ] && ready_at=$t; else r=0; fi
  v=$(probe)
  printf '{"t":%s,"ready":%s,"entry":%s,"canary":"%s","mode":"%s","armed":%s}\n' "$t" "$r" "$e" \
    "$(jq -r .canary <<<"$v")" "$(jq -r .mode <<<"$v")" "$(jq .armed <<<"$v")" >> "$WORK/outage.jsonl"
  [ -n "$ready_at" ] && [ "$t" -ge $((ready_at + 90)) ] && break
  [ "$t" -ge 400 ] && break
  sleep 1
done
absent=$(jq -s '[.[] | select(.entry == 0)] | length' "$WORK/outage.jsonl")
unarmed=$(jq -s '[.[] | select(.armed == false)] | map(.t)' "$WORK/outage.jsonl" | tr -d ' \n')
say "OUTAGE away $OUTAGE_SECONDS s (pod deleted, replicas 1): ready at +${ready_at:-never} s; $(wc -l <"$WORK/outage.jsonl") samples to 90 s past ready; main entry absent in $absent; not armed at t=$unarmed"
say "OUTAGE timeline $(jq -c '[.t, .ready, .entry, .canary, .mode]' "$WORK/outage.jsonl" | tr '\n' ' ' | head -c 4000)"
check "after the outage: the fence is armed again" is "$(probe)" '.armed == true'

# --- the controller at 0 replicas: Kyverno deletes every webhook (a fence state, not a policy bug) ------------
K -n kyverno scale deploy kyverno-admission-controller --replicas=0 >/dev/null
check "scaled to 0: both entries go" wait_for 120 no_entries
v=$(probe); say "PROBE scaled-to-0 $v"
check "scaled to 0: not armed, and the cause names the 0 replicas" \
  is "$v" '.armed == false and .canary == "disarmed" and .mode == "absent" and (.cause | test("scaled to 0 replicas"))'
K -n kyverno scale deploy kyverno-admission-controller --replicas=1 >/dev/null
check "scaled back: both entries return" wait_for 180 both_entries
sleep 5
check "scaled back: armed again" is "$(probe)" '.armed == true'

# --- either policy removed: the armed check must fail (the steward's condition) -------------------------------
K delete clusterpolicy agent-lease-fence >/dev/null
check "main policy removed: its entry goes" wait_for 60 bash -c "! docker exec -i $NODE kubectl --kubeconfig=/etc/kubernetes/admin.conf get validatingwebhookconfiguration kyverno-resource-validating-webhook-cfg -o json | jq -e '[.webhooks[]?.name] | index(\"$MAIN_ENTRY\") != null' >/dev/null"
v=$(probe); say "PROBE main-removed $v"
check "main policy removed, entry absent: the mode Pod is admitted and the fence is NOT armed" \
  is "$v" '.mode == "absent" and .armed == false'
check "main policy removed: the canary still answers through its own entry (the main entry is load-bearing)" \
  is "$v" '.canary == "finegrained"'
K apply -f - <"$WORK/policy.yaml" >/dev/null; wait_for 60 both_entries >/dev/null; set_action Enforce
set_canary_absent
wait_for 60 bash -c "! docker exec -i $NODE kubectl --kubeconfig=/etc/kubernetes/admin.conf get validatingwebhookconfiguration kyverno-resource-validating-webhook-cfg -o json | jq -e '[.webhooks[]?.name] | index(\"$CANARY_ENTRY\") != null' >/dev/null"
v=$(probe); say "PROBE canary-removed $v"
check "canary policy removed: the canary is admitted and the fence is NOT armed" is "$v" '.canary == "disarmed" and .armed == false'
check "canary policy removed: the mode Pod is still refused by the main entry (the canary entry is load-bearing)" \
  is "$v" '.mode == "enforcing" and .mode_entry == "finegrained"'
K delete clusterpolicy agent-lease-fence >/dev/null

# The main policy rendered without matchConditions joins the shared entry: scope shared, never armed (F2(a)).
render no-match-conditions | K apply -f - >/dev/null
set_action Enforce
wait_for 60 bash -c "docker exec -i $NODE kubectl --kubeconfig=/etc/kubernetes/admin.conf get validatingwebhookconfiguration kyverno-resource-validating-webhook-cfg -o json | jq -e '[.webhooks[]? | select(.name == \"validate.kyverno.svc-fail\") | .rules[]?.resources[]?] | index(\"pods\") != null' >/dev/null"
sleep 5
v=$(probe); say "PROBE no-match-conditions $v"
record_text "mode refusal by the shared entry" "$WORK/mode.err"
check "no matchConditions: the probe reports scope shared and is not armed" is "$v" '.scope == "shared" and .armed == false'
K delete clusterpolicy agent-lease-fence agent-lease-fence-canary --ignore-not-found >/dev/null
K apply -f - <"$WORK/policy.yaml" >/dev/null
wait_for 60 both_entries >/dev/null
set_action Enforce

# --- deleting the enrolled namespace under Enforce completes (T4 condition 3) --------------------------------
K -n "$PREMADE" get configmap wcep-agent-fence -o name >/dev/null 2>&1 || gate_write create "$LEASE" active 9
K delete namespace "$PREMADE" --wait=false >/dev/null
check "namespace delete under Enforce: the enrolled namespace goes" wait_for 180 bash -c "! docker exec -i $NODE kubectl --kubeconfig=/etc/kubernetes/admin.conf get namespace $PREMADE >/dev/null 2>&1"
K get namespace "$PREMADE" -o json 2>/dev/null | jq -c '.status.conditions[]? | select(.status == "True")' | while read -r c; do say "NAMESPACE $c"; done

say "RESULT $FAILS failed; work directory $WORK"
exit $(( FAILS > 0 ))
