# Kyverno v1.15.2 shapes for the fence recorder

`policyreports.json` and `events.json` are `kubectl get policyreports.wgpolicyk8s.io -o json` and `kubectl get events -o json` in the enrolled namespace, taken by `test/kind-fence.sh` on Kyverno chart 3.5.2 in kind, in Audit, after three probes and one real would-be denial (`audit-would-deny`). Only `managedFields` was removed. Every name is the fixture's own. `test/test-k8s-guard.py` (`FenceRecord`) reads them.
