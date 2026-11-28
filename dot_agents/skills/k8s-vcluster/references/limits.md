# What a standing vCluster does not provide

Each gap below was checked from inside the standing vClusters with the bot. The runbook's "Standing vClusters" section says which ones have since been closed.

## No DGD CRD and no Dynamo operator

CRDs are not copied from the host. Inside, `get crd` lists nothing until you create one. A workload that needs the host's DynamoGraphDeployment kind needs its own operator and CRDs installed inside the vCluster. That is allowed, and it stays inside, but plan it: pick the operator version, its namespace inside the vCluster, and its teardown.

## No host model cache

The host's shared model cache PVC lives in the host namespace. A pod inside the vCluster cannot mount it, because vCluster syncs PVCs from the vCluster to the host, not the other way. Download weights into a PVC you create inside the vCluster, using the storage class from the profile, and delete it at the end.

## No MNNVL yet

The vCluster serves the DRA APIs (`deviceclasses`, `resourceclaims`, `resourceclaimtemplates`, `resourceslices`), but lists no DeviceClasses. Syncing DeviceClasses from the host, ResourceClaims and ResourceClaimTemplates to the host, and ComputeDomains to the host is off. So an MNNVL ComputeDomain or an IMEX channel cannot be claimed from inside. Turning that sync on is the owner's change to the vCluster, not the bot's.

## Batch scheduler queues are bypassed

The vCluster schedules its own pods and writes `spec.nodeName` before the pod reaches the host. A host policy that assigns GPU pods to a batch scheduler and its queue still labels the pod, but the batch scheduler never places it. The pod skips that scheduler's queueing and quotas. The runbook names the clusters where this applies. GPU use there needs its own test and record.

## Host-side limits still apply

Synced pods are host pods in the host namespace. The host's admission policies, image pull rules, taints, the GPU reaper and any quota on the host namespace all apply to them.
