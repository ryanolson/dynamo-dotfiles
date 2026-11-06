---
name: slurm
description: Build, test, and run on a Slurm cluster through srun, sbatch, and pyxis/enroot containers, where the login node may be a different architecture from the compute nodes. Use before any build, test, or GPU work on a host that has sbatch, and when a job fails to start or a container fails to launch.
---

# Slurm clusters

A Slurm login node is a submission host, not a build host. This skill holds the rules that hold on every cluster. The facts of one cluster, such as its account, its partitions, its image paths, and its model cache, live in that project's own agent file. Read the project agent file as well; where the two disagree, the project file wins.

## Detect where you are

Run this at session start on an unfamiliar host.

```bash
command -v sbatch >/dev/null && echo "slurm host"
echo "arch=$(uname -m) node=${SLURMD_NODENAME:-submit-host} job=${SLURM_JOB_ID:-none}"
```

- `sbatch` is missing: this skill does not apply.
- `SLURMD_NODENAME` is set: `slurmd` started this process, so you are on a compute node. Build and test here.
- `SLURMD_NODENAME` is empty: you are on the submit host. Do not build, do not run tests, and do not run Python that imports CUDA. Dispatch the work with `srun` or `sbatch`.

`SLURM_JOB_ID` does not answer this. Inside an allocation it is set on the submit host too, so it cannot tell a login node from a compute node. Measured on a GB200 cluster, 2026-11-06: `salloc ... bash -c 'hostname; uname -m'` ran on the x86_64 login node with `SLURM_JOB_ID` set and `SLURMD_NODENAME` unset, while an `srun` step from the same allocation ran on an aarch64 node with both set. A cluster with `LaunchParameters=use_interactive_step` moves an interactive `salloc` shell onto a compute node, but a command passed to `salloc` still runs on the submit host.

When the signal is ambiguous, treat the host as a login node. Cross-check with `uname -m` against the compute architecture the project's agent file records.

The login node and the compute nodes often differ in architecture. One cluster has x86_64 login nodes and aarch64 Grace compute nodes. A build on the login node then produces binaries that no compute node can execute.

## Learn a cluster before you use it

Do this once per cluster and write the answers into the project agent file.

```bash
sinfo -o '%P %a %l %D %G %f'          # partitions, limits, generic resources, features
scontrol show partition <name>        # defaults, max walltime, allowed accounts
sacctmgr show assoc user=$USER format=Account,Partition,QOS
srun --help | grep -i container       # which pyxis flags this version has
scontrol show topo                    # topology, where the cluster defines one
srun -p <partition> -N1 -n1 -t 2 bash -c 'uname -m; echo $HOME; ls -d $HOME; df -h /tmp /raid 2>/dev/null'
```

That last probe answers three questions at once: the compute architecture, whether `$HOME` exists on a compute node, and which node-local scratch paths exist and how large they are.

GPU request syntax is per cluster. Some partitions reject a job that asks for no GPU, so they need `--gres=gpu:1` even for an import or a build. Others hand out whole nodes and reject `--gpus-per-node` with `Invalid generic resource`. Read `%G` from `sinfo` and try one short job before you write the flag into a script.

## The rules that hold everywhere

**Build in a job step.** Every compile, test, and benchmark goes through `srun` or `sbatch`. The only work that belongs on the login node is editing files, submitting jobs, reading logs, and fetching from the network.

**Never hardcode an architecture in a path.** A shared `$HOME` is mounted on hosts of both architectures. Select with `uname -m`, keep tools under `~/.local/<arch>/`, and give each architecture its own `CARGO_TARGET_DIR` and `CARGO_HOME`. A wrong-architecture binary on `PATH` fails with `Exec format error`, and the failure can hit a tool the build only calls indirectly.

**Keep caches on the shared parallel filesystem, not in `$HOME`.** The home directory is usually a small NFS quota, and it is a different filesystem from the work tree, so a hardlinking cache such as `uv`'s cannot work from there. Point the cargo, uv, pip, and Hugging Face caches at a directory on the parallel filesystem.

**Export every variable a child process must see.** A bare shell variable does not reach the engine or worker that the launcher starts.

**Treat compute nodes as offline.** Pre-stage model weights and warm the source caches where there is network, with `cargo fetch` and the like, then set the offline flag at run time. Where a transfer tool is killed on the login node, fall back to the plain downloader and limit its workers.

Fetch on the login node, but build in the job. A command that materializes an environment rather than downloading sources, such as `uv sync`, produces architecture-specific artifacts. A work tree shared between architectures usually has one `.venv`, so running it on the login node replaces the compute node's environment with one that cannot run there. Give each architecture its own environment, or run the sync inside the job step.

**Node-local paths disappear.** `/tmp` and `/raid` on a compute node are gone when the job ends. Copy anything you want to keep onto the parallel filesystem.

## Containers through pyxis and enroot

Pyxis runs a squashfs image that enroot produced. It also accepts a registry reference, such as `--container-image='<registry>#<repo>:<tag>'`, but then it imports the image again for every job. Pre-stage a `.sqsh` on the parallel filesystem instead. Enroot does not emulate: an image of the wrong architecture fails at `enroot-switchroot` with `Exec format error`.

- Import for the target architecture: `enroot import -a aarch64 -o out.sqsh 'docker://<registry>#<repo>:<tag>'`. Build elsewhere with `docker buildx build --platform linux/arm64`.
- Run both import and bake on a compute node of the target architecture, with the enroot temp directory on node-local scratch. The parallel filesystem rejects the file-capability xattrs that `enroot create` sets, and login-node `/tmp` is usually too small.
- Use a clean `PATH` for the import. A login shell profile leaks tools of the other architecture into the job.
- Registry credentials live in `~/.config/enroot/.credentials`, mode 600. Enroot rejects a looser file and then authenticates anonymously, which shows up as a 401 or 403. For NGC the username is the literal string `$oauthtoken`, so quote it. Take the key through `openv` or `fleet secret get`; never write it into a script.
- Where compute nodes have no `$HOME`, stage the credentials file onto each node and set `ENROOT_CONFIG_PATH` to that directory.

**The container mounts nothing you do not name.** Pass `--container-mounts` for the source tree, the build directory, the caches, and the model cache. A tool whose cache defaults to `$HOME/.cache` writes into the overlay, which vanishes with the job, and the failure reads like a build error rather than a lost directory.

**The overlay is ephemeral.** A `pip install` inside the container is lost when the allocation ends. Keep installs on a mounted path, or bake them into an image.

**Save an image from a clean one, on one node and one task.** `--container-save` writes whatever is on the filesystem at exit, so save from a fresh `--container-image` rather than from a container a run has dirtied, and clean the package and pip caches first. More than one task saving to the same path corrupts the file. Write a new versioned file; never overwrite the image a running job is using. Record the same steps in a Dockerfile or a build script under version control, so the image can be rebuilt.

**A wheel is architecture specific.** A dependency published only for x86_64 cannot install on an aarch64 node, and the error usually names the installation plan rather than the architecture.

## Running steps

- `--overlap` lets a second `srun` step share an allocation that is already busy. Without it the step waits for resources it will never get.
- `--container-name=<name>` keeps the unpacked container on the node, so later steps in the same job start warm. The cache is scoped to the job, so it is lost when the allocation ends.
- Restate `--container-mounts` on each step rather than relying on the named container to carry them.
- Where the cluster groups nodes into topology domains, request one domain explicitly, for example `--segment=N`, so a multi-node run stays on the fast fabric.
- Redirect build and test output to a log file on the parallel filesystem and read the tail. Do not stream a full build into the conversation.
- Check the remaining walltime with `squeue -h -j <jobid> -o %L` before starting a long step.
- Never use `--pty` for a non-interactive command.
- Respect the cluster's job-name policy, where it has one.

Run tests under `timeout` as `AGENTS.md` says. A job that hangs holds the allocation until the walltime runs out.

## Hold a node only where holding is free

A GPU node left idle while an agent thinks is the most expensive mistake available here, so read the scheduler's own answer before you decide to hold one.

```bash
scontrol show partition <name> | grep -oE 'OverSubscribe=[A-Z]+|PriorityTier=[0-9]+|PreemptMode=[A-Z,]+|GraceTime=[0-9]+'
scontrol show config | grep -iE 'PreemptType|PreemptMode|PreemptExemptTime'
```

- `OverSubscribe=EXCLUSIVE` means every job takes a whole node with all of its GPUs. There is then no such thing as a CPU-only build job, however little of the node a build uses.
- A partition with the lowest `PriorityTier` and `PreemptMode` set to something other than `OFF` is the preemptible tier. A job there yields the node as soon as a higher tier wants it, so holding one costs other people nothing.
- `PreemptExemptTime` and `GraceTime` say how much warning a preempted job gets. With `PreemptMode=CANCEL` the job then dies, so any tool that holds an allocation must expect it to vanish, notice, and resubmit.

From that, three rules:

- **An idle holder belongs on the preemptible tier**, with a walltime matched to the session rather than the partition maximum, and a `scancel` as soon as the work stops.
- **Work that must not be interrupted stays off it.** An image import, a `--container-save`, and a benchmark all lose everything on a cancel, and a save can leave a corrupt image behind.
- **Unattended work goes in one job per unit of work**, not one per command. Put the build, the test, and the gates in a single job rather than three allocations.

Compare partitions before you submit, and do not trust the estimate alone:

```bash
sbatch --test-only -A <acct> -p <partition> -N1 -t 01:00:00 --wrap true
```

It prints a predicted start time without queueing anything, which is the cheapest way to see which partition is worth using. It does not account for nodes held back for a higher-priority partition, so a partition it calls immediate can still pend. Check `sinfo -p <partition> -o '%t %D'` for real idle nodes, and read the pending reason with `squeue -j <jobid> -o '%T %R'`.

## The inner loop

Two shapes work. Pick by how often you rebuild.

**A job per action.** The simplest form, and what the validated harnesses in this owner's workspaces do: one `srun` that allocates a node, runs one step, and exits. Good for an unattended build, a test, or a benchmark.

**One held allocation, many steps.** Submit a holder job, then run each build and test as an `--overlap` step against it with a named container. This avoids paying the queue wait and the cold container start on every edit. Hold it on the preemptible tier only, as above. `references/held-allocation.md` has the pattern. It is reported to work and has not been measured on this owner's clusters, so measure the cold and warm start times before you build a workflow on it.

Either way, mount the source and build incrementally. Do not rebuild or save an image to test a code change. An image rebuild is for a system-level dependency only.

## When something fails

`references/troubleshooting.md` maps the common symptoms to their causes. Read it before resubmitting a job. Do not loop resubmitting.

## What to write down

A cluster fact you had to discover belongs in the project's agent file, not only in this skill: the account and partitions, the GPU request syntax, the image paths, the cache paths, the node-local scratch path, and any flag the cluster rejects. Keep this skill to what holds on every cluster.
