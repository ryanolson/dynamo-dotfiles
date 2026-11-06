# Slurm and pyxis symptoms

| Symptom | Cause | Fix |
| --- | --- | --- |
| `enroot-switchroot: failed to execute: /bin/bash: Exec format error` | The image architecture does not match the node | Import with `-a <arch>` or build with `--platform`. Check with `uname -m` inside the container. |
| `Exec format error` from a tool the build calls | A wrong-architecture directory is on `PATH` from a shared home | Select the directory from `uname -m`. Use a clean `PATH` in the job. |
| `401 Unauthorized`, `403 Forbidden`, or `Authenticating with user: <anonymous>` | The credentials file is missing, unreadable, or too permissive on the compute node | `~/.config/enroot/.credentials`, mode 600. Where the node has no `$HOME`, stage it and set `ENROOT_CONFIG_PATH`. |
| `Cannot find GPU specification`, or a QOS error about GRES | The partition requires a GPU request | Add the cluster's GPU flag, even for an import or a build job. |
| `Invalid generic resource` | The cluster hands out whole nodes and rejects a per-node GPU count | Drop the GPU flag. Read `%G` from `sinfo`. |
| `enroot-mount: failed to mount: <path>: No such file or directory` | The mount source does not exist on the compute node | Use a path on the parallel filesystem and create it first. |
| A named container fails to start every time | A cached container on that node is corrupt | `enroot list`, then `enroot remove <name>` on that node, or use a new name. |
| A saved image is corrupt | `--container-save` ran with more than one task or node | Rerun with `-N1 -n1`. |
| A saved image is unexpectedly large | The save captured scratch files a run left behind | Save from a clean `--container-image` and clear the package and pip caches before exit. |
| An overlapping `srun` hangs | The step cannot get resources | Add `--overlap`, or ask for fewer CPUs or GPUs in the step. |
| Pyxis fails on a minimal image | Pyxis needs `/bin/sh` inside the container | Use an image that has a shell. |
| A gated test reports `ignored`, or a cache looks empty | The cache defaulted to `$HOME/.cache`, which the container does not mount | Point the cache at a mounted path. |
| `uv` fails to determine an installation plan | A dependency has no wheel for this architecture | Remove it from the step, or build it for the architecture. |
| A download is killed on the login node | The transfer tool exceeds the login node's limits | Disable the accelerated transfer, limit the workers, and run it under `nice`. |
| `enroot import` stalls for hours | Storage or cluster specific | Ask the cluster's support channel. Try a different node type. |
| The wrong GPU appears inside the container | `ENROOT_RESTRICT_DEV` interacting with `CUDA_VISIBLE_DEVICES` | Do not force `ENROOT_RESTRICT_DEV=y` unless the cluster requires it. |
