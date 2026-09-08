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

The private environment is a correctness rule and stays: two processes
consuming an environment another process just wrote raced NFS close-to-open
consistency (the first live steward validation spawned a binary that was not
yet visible), and the orchestrator never executes session-authored
entrypoints from a workspace environment. The per-job cache was hygiene, not
a rule: caches on the shared filesystem once filled the scratch inode quota
(2026-09-03, per-run session caches under 139 ended runs), and a cache that
dies with the job leaves nothing to reap.

The cost shows on a torch target. A speedrun eval installs torch and its
CUDA libraries, about 2.5 GB, on every job; on Torch's network that is a
minute, on a workstation several. The heavier the stack the worse it gets,
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
  `uv sync --frozen --no-install-project` into a throwaway environment on the
  tick host, with `UV_CACHE_DIR` pointing at that cache. The tick host has the
  network and runs trusted code. It does this when the target's lockfile hash
  changes and at most once per tick; the lockfile hash is recorded beside the
  cache. A failed warm is logged and the cache stays as it was.
- **Each job seeds its private cache from it by copy**, in the job script,
  outside the jail, before `uv` runs: `cp -a` of the seed into `$SCRATCH/cache`
  when the seed exists. A copy, never a bind and never hardlinks: the job's
  cache is its own from the first byte, so nothing a job does reaches the
  seed or another job. On node-local disk the copy is seconds; on a
  workstation it is one local copy instead of one download.
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
gains one knob:

```yaml
image: hf://outerloop-science/speedrun-image@main   # or a path on the cluster
```

- The kernel resolves it once per tick (the Hub cache for `hf://`, the path as
  given otherwise), verifies the published checksum when there is one, and
  hands the path to every job of that target: evals, launches, sessions. The
  deployment's image stays the default for targets that name none.
- The image carries the heavy base — torch and CUDA for speedrun — so `uv
  sync` inside it installs the small remainder from the seed cache, and the
  target maintains its own recipe (`containers/` in its repo, built by a
  workflow like the kernel's), the way it maintains its lockfile.
- Trust: the image is target-maintained content that runs the target's own
  code; it changes nothing about what a job may reach. A target that ships a
  hostile image hurts only its own evals, which it could already do with its
  eval command.

## Order

1. The seed cache, kernel side, with the warmer's tests and a job-script
   test that a present seed is copied and an absent one is skipped.
2. The `image:` knob and its resolution.
3. Speedrun's image recipe, in that repository.

Open: whether the warmer should also seed the *session's* environment (the
author's `uv run` in the workspace pays the same download once per session),
and what to do about a target whose lockfile names packages that need
building from source on the tick host.
