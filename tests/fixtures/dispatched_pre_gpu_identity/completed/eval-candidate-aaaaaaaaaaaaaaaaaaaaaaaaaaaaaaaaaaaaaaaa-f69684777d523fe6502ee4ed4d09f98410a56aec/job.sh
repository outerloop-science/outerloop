#!/bin/sh
set -u
EV=tests/fixtures/dispatched_pre_gpu_identity/completed/eval-candidate-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-f69684777d523fe6502ee4ed4d09f98410a56aec
REPO=/fixture/repo
SCRATCH="${SLURM_TMPDIR:-${TMPDIR:-/tmp}}/dispatch-eval-$$"
TREE="$SCRATCH/tree"
mkdir -p "$SCRATCH/cache" "$SCRATCH/home" "$SCRATCH/work" "$TREE"
cleanup() { rm -rf "$SCRATCH"; git -C "$REPO" -c core.hooksPath=/dev/null -c core.sshCommand=false -c credential.helper= -c protocol.allow=never -c protocol.https.allow=always -c protocol.file.allow=always -c core.fsmonitor= -c core.quotePath=false worktree prune >/dev/null 2>&1 || true; }
trap 'cleanup' EXIT
trap 'echo 143 > "$EV/exit-code"; cleanup; trap - EXIT; exit 0' TERM INT HUP
[ -s "$EV/command.txt" ] || { echo 96 > "$EV/exit-code"; exit 0; }
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
export GIT_CONFIG_COUNT=1
export GIT_CONFIG_KEY_0=core.attributesFile
export GIT_CONFIG_VALUE_0=/dev/null
if git -C "$REPO" -c core.hooksPath=/dev/null -c core.sshCommand=false -c credential.helper= -c protocol.allow=never -c protocol.https.allow=always -c protocol.file.allow=always -c core.fsmonitor= -c core.quotePath=false worktree add --detach "$TREE" aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa >> "$EV/setup.log" 2>&1; then rm -f "$TREE/.git"; else echo 97 > "$EV/exit-code"; exit 0; fi
export UV_CACHE_DIR="$SCRATCH/cache" UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT="$SCRATCH/cache/venv"
export APPTAINERENV_UV_CACHE_DIR="$UV_CACHE_DIR" APPTAINERENV_UV_LINK_MODE=copy APPTAINERENV_UV_PROJECT_ENVIRONMENT="$UV_PROJECT_ENVIRONMENT"
export SEED=7 APPTAINERENV_SEED=7
apptainer exec --containall --cleanenv --nv --bind "$TREE:$TREE" --home "$SCRATCH/home:$SCRATCH/home" --bind "$SCRATCH/cache:$SCRATCH/cache" --pwd "$TREE" --workdir "$SCRATCH/work" /img.sif sh -c "$(cat "$EV/command.txt")" > "$EV/stdout" 2> "$EV/stderr"
echo $? > "$EV/exit-code"
exit 0
