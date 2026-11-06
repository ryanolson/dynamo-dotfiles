# One held allocation, many steps

Reported to work on NVIDIA GB200 clusters, from a synthesis of internal reports in November 2026. It has not been measured on this owner's clusters. Measure the cold and the warm container start before you build a workflow on it, and record the numbers in the project's agent file.

The idea: the queue wait and the first unpack of a large image cost far more than a build does. Pay them once per session instead of once per edit. One report gives about 13 seconds cold and about 1 second warm for a 29 GB image. The warm cache belongs to the job, so ending the allocation throws it away.

## Hold the allocation

```bash
JID=$(sbatch --parsable -A "$ACCT" -p "$PART" -N1 -t 04:00:00 \
      -J "$ACCT-dev.agent" -o "$LOGS/holder-%j.out" --wrap 'sleep infinity')
until [ "$(squeue -h -j "$JID" -o %T)" = RUNNING ]; do sleep 10; done
```

Add the cluster's GPU request where the partition needs one. A non-interactive holder job suits an agent better than `salloc --pty`. `salloc --no-shell` is equivalent.

## Warm the named container on every node

```bash
srun --jobid="$JID" --overlap -N1 --ntasks-per-node=1 \
  --container-image="$IMG" --container-name=dev \
  --container-mounts="$MNT" --no-container-mount-home true
```

This makes sure the layers are present before the first real step.

## Each step

```bash
COMMON="--jobid=$JID --overlap -N1 -n1 --container-name=dev --container-mounts=$MNT --no-container-mount-home"
srun $COMMON bash -lc 'cmake -S /src -B /build -G Ninja && ninja -C /build' > "$LOGS/build.log" 2>&1
srun $COMMON bash -lc '/build/bin/my_test' > "$LOGS/test.log" 2>&1
```

Add the GPU request to the step where the cluster hands out GPUs per step rather than per job. For several ranks, use `--ntasks-per-node` with the cluster's MPI flag; all ranks on a node share one container filesystem, so no two may write the same path inside it.

## A debug shell

```bash
srun --jobid="$JID" --overlap -N1 -n1 --container-name=dev --pty bash
```

This attaches another step to the same container, which is how to reach a process another step started.

## Tear down

```bash
scancel "$JID"
```

The named container and the node-local cache go with the job. Anything on the parallel filesystem stays.
