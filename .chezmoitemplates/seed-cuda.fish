# Per-machine fish config (conf.d/local.fish) — "cuda" seed.
# Created once by chezmoi, then left alone — edit freely. Not synced.

# CUDA toolkit, if present on this machine
if test -d /usr/local/cuda/bin
    fish_add_path /usr/local/cuda/bin
    set -gx CUDA_PATH /usr/local/cuda
    set -gx CUDA_HOME /usr/local/cuda
end
