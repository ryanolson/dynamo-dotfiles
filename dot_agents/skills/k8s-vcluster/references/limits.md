# What a standing vCluster does not provide

Each gap below was checked from inside the standing vClusters with the bot. The runbook's "Standing vClusters" section says which ones have since been closed.

## The Dynamo operator is installed per vCluster, not copied from the host

CRDs are not copied from the host. Inside, `get crd` lists nothing until something installs one. A standing vCluster that runs DGDs has its own Dynamo platform release in the vCluster namespace `dynamo-system`. The runbook's table says which vClusters have one and its version. It is standing: do not uninstall it at the end of a task. Installing it where it is missing is allowed, inside the vCluster only: `helm install dynamo-platform <chart .tgz> -n dynamo-system --create-namespace`, with the chart's defaults (no NATS, etcd, Grove or KAI). Check the chart file's sha256 against the one the runbook records. See `SKILL.md`, "Run a DGD".

## The host model cache is not a PVC inside

The host's shared model cache PVC lives in the host namespace. A pod inside the vCluster cannot name it: vCluster syncs PVCs from the vCluster to the host, never the other way, and the PV is already bound to the host claim. Two ways work without changing the vCluster:

- **The cache is NFS on the host** (its PV has `spec.nfs`): mount the same export in the pod as an inline `nfs` volume with `readOnly: true`. Read the server and path from the host PV with the bot's kubeconfig. No copy and no new storage.
- **The cache is a CSI volume** (`spec.csi`, such as a managed Lustre service): an inline volume is not possible. Create a PVC in the task's namespace with the storage class from the profile, seed it from Hugging Face with a CPU pod, and let the namespace delete take it. A 1.5 GB model took 9 seconds to download.

Never mount the cache read-write from inside, and never create a host PV for it.

## No MNNVL

The vCluster serves the DRA APIs (`deviceclasses`, `resourceclaims`, `resourceclaimtemplates`, `resourceslices`), but lists no DeviceClasses. Syncing DeviceClasses from the host, ResourceClaims and ResourceClaimTemplates to the host, and ComputeDomains to the host is off. So an MNNVL ComputeDomain or an IMEX channel cannot be claimed from inside. Those syncs, and pod patches, are licensed vCluster features: the standing vClusters run without a license, and the vCluster docs say a syncer configured with a licensed feature and no license refuses to start. Changing that is the owner's decision, not the bot's.

## Batch scheduler queues are bypassed

The vCluster schedules its own pods and writes `spec.nodeName` before the pod reaches the host. A host policy that assigns GPU pods to a batch scheduler and its queue still labels the pod, but the batch scheduler never places it. The pod skips that scheduler's queueing and quotas. The runbook names the clusters where this applies. GPU use there needs its own test and record.

## Host-side limits still apply

Synced pods are host pods in the host namespace. The host's admission policies, image pull rules, taints, the GPU reaper and any quota on the host namespace all apply to them.
