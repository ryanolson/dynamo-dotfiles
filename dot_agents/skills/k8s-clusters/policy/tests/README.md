# The fence policy's CLI tests

`kyverno test` suites for `../agent-lease-fence.template.yaml`. `test/test-k8s-fence.py` renders the template
with the made-up names below into `policy.yaml` beside each `kyverno-test.yaml`, then runs the Kyverno CLI
named by `$KYVERNO_CLI` (v1.15.2). The CLI never calls an API server, so each resource's `gate` (the policy's
`apiCall` context) comes from `values.yaml`; the kind fixture (`test/kind-fence.sh`) runs the real read.
Webhook match conditions are the API server's, not the CLI's: the kind fixture tests them too.

Made-up names: pre-made namespace `dyntest-alice-agent`, prefix `dyntest-alice-`, bot user `bot-example-agent`,
owner `alice-example`, contact `example-contact`, runbook section `example runbook section 10`.

- `scope/`: who is matched at all (the skip cases live only here).
- `enforce/`: Pods and bot roots under the gate; `controller/` has a non-bot user.
- `gate/`: the gate ConfigMap's rules; `other-user/` has a non-bot user.
- `attribution/`: every rule's message names the owner, the contact and the off-switch (`checks:`).
