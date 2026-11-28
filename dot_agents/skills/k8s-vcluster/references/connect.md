# Connect to a standing vCluster

These commands use only the bot's kubeconfig and the pinned kubectl. Run them in bash. Set the four values from the profiles (see `SKILL.md`, "Where they are"); the names below are placeholders.

```bash
export KUBECONFIG=<[bot] kubeconfig from the profiles>   # the bot's file; never ~/.kube/config
C=<[clusters.<name>] context>
NS=<[bot] namespace_prefix>vcluster
VC=<[bot] developer>

D=$(mktemp -d); chmod 700 "$D"          # or a directory under $BB_THREAD_STORAGE, mode 700

# 1. The vCluster's kubeconfig. It is a cluster-admin credential for the vCluster: never print it.
( umask 077; kubectl --context "$C" -n "$NS" get secret "vc-$VC" -o jsonpath='{.data.config}' | base64 -d > "$D/vc.yaml" )
test -s "$D/vc.yaml" || echo "could not read vc-$VC"

# 2. A port-forward to its API, on a port the OS picks.
kubectl --context "$C" -n "$NS" port-forward "svc/$VC" :443 > "$D/pf.log" 2>&1 &
PF=$!
for i in $(seq 1 30); do
  P=$(sed -n 's/^Forwarding from 127.0.0.1:\([0-9]*\).*/\1/p' "$D/pf.log" | head -1)
  [ -n "$P" ] && break; sleep 1
done

# 3. Every vCluster command.
vk() { kubectl --kubeconfig "$D/vc.yaml" --server "https://localhost:$P" "$@"; }
vk auth can-i '*' '*' --all-namespaces      # yes
vk get nodes                                # the host's nodes, synced read only

# 4. When done.
kill "$PF"; rm -rf "$D"
```

`kubectl version` prints the client's version first; read `serverVersion` when you check the vCluster's own version.

## Checks that tell you the path works

Measured from a host with the bot's kubeconfig on all three standing vClusters:

| Check | Expected |
|---|---|
| `kubectl --context "$C" -n "$NS" auth can-i get secrets` | `yes` |
| `kubectl --context "$C" -n "$NS" auth can-i create pods/portforward` | `yes` |
| `vk auth can-i '*' '*' --all-namespaces` | `yes` |
| a CRD created with `vk apply` | absent from the host: `kubectl --context "$C" get crd <name>` says `NotFound` |
| a namespace created with `vk create namespace` | absent from the host |

If the Secret read or the port-forward is refused, stop and report: the namespace may be missing its opt-in labels, or the owner may have removed the vCluster.

## Keep the forward alive

A port-forward dies when the vCluster pod restarts or the bot's certificate is renewed. Before a batch of `vk` calls, check `kill -0 "$PF"`, and start a new forward if it is gone. The local port changes each time, so read it again.

## Two clusters at once

Use one directory, one file and one port-forward per cluster. Every vCluster's kubeconfig names its context `kubernetes-super-admin@kubernetes`, so merging two files into one kubeconfig would mix them up.
