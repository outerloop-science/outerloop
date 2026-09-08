# Dependency caches for evals, and the target image

Where an eval's dependencies come from, why every job downloads them today,
and the two changes that stop it without sharing writable state between
untrusted jobs.

## Today

Every dispatched eval and every author launch runs the same job script
(`dispatch.write_eval_job`). It builds a private virtual environment on
node-local scratch from the target's lockfile and keeps uv's cache beside it;
both die with the job. Local compute runs the identical script, so the same
holds on a workstation.

The private environment is a correctness rule and stays: two processes once
used one environment at the same time, and a validation run started a binary
before the other process's write was visible on NFS. The orchestrator also
never executes session-authored entrypoints from a workspace environment. The per-job cache was hygiene, not
a rule: caches on the shared filesystem once filled the scratch inode quota
(2026-09-03, per-run session caches under 139 ended runs), and a cache that
dies with the job leaves nothing to reap.

The cost shows on a torch target. A speedrun eval installs torch and its
CUDA libraries, about 2.5 GB, on every job; on Torch's network that is a
minute; on a workstation, several minutes. The heavier the stack the worse it gets,
and torch is the common case, not the exception.

## The constraint that shapes the fix

An eval runs agent-authored code inside the jail with its cache directory
bound read-write, because `uv` inside the jail writes the cache. A cache
shared read-write between jobs is therefore shared writable state between
untrusted parties: one eval could tamper with a cached wheel and a later
eval — another agent's gate measurement — would install it. That is the
same class of problem the private environment closed, and it rules out the
obvious fix, one `UV_CACHE_DIR` for everyone.

So the rule: **jobs never write shared state.** Anything shared is written by
the kernel, on a trusted host, and jobs only read it.

## Change 1: a kernel-warmed seed cache

- **The kernel warms one cache per target**, `<state root>/eval-cache/<target>`
  on Torch and `~/.outerloop/eval-cache/<target>` in local mode, by running
  `uv sync --frozen --no-install-project --no-build` into a throwaway
  environment on the tick host, with `UV_CACHE_DIR` pointing at that cache.
  `--no-build` is the trust line: the lockfile is target content, and a source
  distribution's build backend is target code, so the warmer downloads and
  unpacks wheels only and executes nothing of the target's. A lockfile that
  needs a build is not warmed (logged once per lockfile hash) and its jobs
  install as today. The warm runs when the target's lockfile hash changes and
  at most once per tick; the hash is recorded beside the cache. A failed warm
  is logged and the cache stays as it was.
- **Each job seeds its private cache from it by copy**, in the job script,
  outside the jail, before `uv` runs: the seed's *contents* are copied into
  the job's `$SCRATCH/cache` (`cp -a seed/. "$SCRATCH/cache"/`, the layout uv
  expects) when the seed exists. A copy, never a bind and never hardlinks:
  the job's cache is its own from the first byte, so nothing a job does
  reaches the seed or another job. On node-local disk the copy is seconds; on
  a workstation it is one local copy instead of one download.
- **The jail is unchanged.** The job's cache is still `$SCRATCH/cache`, bound
  read-write as today, and dies with the job.
- **Inodes stay bounded.** One seed per target holds one copy of each wheel
  version the lockfile names, tens of thousands of files, not one copy per
  run. The warmer prunes with `uv cache prune` after each sync.
- **Local mode is the same code**, with the seed under the state root; the
  local loop warms it on its own tick.

What this does not do: it does not make a job faster than one local copy of
its dependencies, and it does not help a target whose lockfile changes every
run. Both are what the image is for.

## Change 2: a target image

The architecture note already reserves a per-target Apptainer image as where
a repository's dependency world belongs; today one shared agent image serves
every target and the deployment names it (`OUTERLOOP_IMAGE`). The contract
gains the knob the architecture note and the roadmap already name,
`environment.container`, pinned to content:

```yaml
environment:
  container: hf://outerloop-science/speedrun-image@<revision sha>   # or a path plus sha256
```

- **Evals and launches only.** The kernel resolves the reference once per
  tick (the Hub cache for `hf://`, the path as given otherwise), verifies the
  digest, and hands the path to that target's eval and launch jobs. Sessions
  keep the deployment's image: the session harness places the author's model
  credential inside its container, and a target-maintained image must never
  be where that credential lands. The deployment's image (`OUTERLOOP_IMAGE`) is
  the default for targets that name none — the contract as it stands since
  the agent image shipped; the architecture note's older line about a
  uv-managed environment for such targets is updated to say so.
- **What the image carries is a seed cache, not an installed torch.** Every
  job builds its private environment from the lockfile regardless of what is
  installed in the image, so preinstalled packages would be installed again.
  The image instead ships the target's warmed uv cache at a known path, and
  the job seeds its scratch cache from there by copy — the same copy as
  Change 1, from a read-only source inside the image instead of the state
  root. That works with the private environment rather than around it, and
  it makes the dependency set part of a pinned artifact: a re-run months later
  installs the same bytes.
- **Digests, not tags.** A mutable reference can change under a measured
  result; the knob takes a revision or a checksum and the kernel refuses a
  bare tag.
- The target maintains its own recipe (`containers/` in its repo, built by a
  workflow like the kernel's), the way it maintains its lockfile. Trust: the
  image runs the target's own code in the eval jail and reaches nothing a job
  could not already reach; a hostile image hurts only its own evals.

## Order

1. The seed cache, kernel side: the `--no-build` warmer with its tests, and a
   job-script test that a present seed's contents are copied into the job's
   cache and an absent seed is skipped.
2. The `environment.container` knob (evals and launches, digest-pinned) and
   the in-image seed path.
3. Speedrun's image recipe, in that repository.

Open: whether the warmer should also seed the *session's* environment (the
author's `uv run` in the workspace pays the same download once per session),
and what to do about a target whose lockfile names packages that need
building from source on the tick host.
