# Where review compute runs

**Status: proposal (2026-08-19).** `judge-placement.md` settled *which home*
each judge belongs to — the reviewer is an external service that stays a
standalone GitHub-side workflow; the verifier is the second half of the climb
transaction and lives in the orchestrator. This note settles a narrower
question that one left open: *where the reviewer's runner physically executes.*
The trigger stays a GitHub workflow (`pull_request_target` + the
`outerloop:review` label); what can move is the compute behind it.
Pairs with `reviewer-infra.md` (the seams and the threat model) and
`dispatcher.md` (the containment this reuses).

## Why now

Two forces made the runner's placement a live question the same week:

- **Cost.** The private repo's free GitHub Actions minutes (3000/month) ran
  out, so hosted-runner minutes now cost money. Multi-round advisory review
  across the lab's repos burns minutes fast — every re-trigger is a fresh
  session job plus a posting job. This force is **temporary**: at the public
  flip (`public-surface.md`) public repos get unlimited free minutes and the
  cost pressure disappears. Cost alone is not a reason to re-architect — it is
  a reason to *have the option* before the flip.
- **Fragility.** The advisory path is flaky under our own load. The
  unauthenticated hermes-agent clone hits GitHub's 429 rate limit under rapid
  re-triggers, and sustained hammering fails both the session job *and* the
  posting job — it has been down cross-repo (autoresearch and jepa-agent) at
  the same time. Recovery needs a cooldown (~25–30 min), and there is no lever
  to raise the limit: GitHub owns it. This force is **not** temporary and does
  not go away at the public flip.

Neither force touches the merge gate. The advisory review is **not** a required
status check — `main` is gated by `ci` and conversation-resolution only — so
the review being down never blocks a merge. That is the load-bearing fact for
everything below: review placement is a quality-of-service question, not a
correctness one, which is what makes moving it low-risk.

## Review compute is API-bound

A review session *thinks*: it calls the reviewer API (Claude, or terra via
hermes/OpenRouter), reads the PR HEAD tree with read-only file tools, and posts
findings. It needs outbound network and the reviewer keys. It needs **no GPU
and no Slurm queue.**

That is the whole difference from the dispatcher's eval jobs. The dispatcher
exists because measuring wants GPU and long walltime, so measuring becomes a
*job* on the cluster queue. Review wants neither. It does not belong in the
dispatcher's job path; it is a small contained process that can run anywhere
with network and keys — a GitHub-hosted runner, or a long-lived daemon on lab
hardware.

## One tick, not two cadences

The worry that surfaced this: reviews want a fast response (post within a
minute of the label), but the climber ticks slowly (~30 min between launches).
Running review on its own fast clock next to the climb's slow clock is two
schedulers to reason about.

The fix is to keep **one fast tick** and demote the 30-minute value from a
*clock* to a *launch throttle*. The tick fires often; each climb run carries
its own minimum launch spacing and budget gate, and reviews carry theirs
(near-zero spacing). The tick is a scheduler that wakes frequently and launches
whatever is due under its throttles — a labelled PR launches its review almost
immediately, a climb stays spaced from the last one. No second daemon, no
second clock; the two cadences are two throttle values on one tick.

## Two placements for the runner

### A. GitHub-hosted ephemeral (today)

A fresh hosted runner per trigger. Isolated by construction, no infra to own,
and the natural home for anything that must run on every PR from anyone.
Downsides are exactly the two forces above: it meters minutes on the private
repo (free after the public flip), and it is fragile under our re-trigger load
because each round re-clones the agent unauthenticated and can trip 429.

### B. Self-hosted contained daemon on cluster0

cluster0 workstations have docker/apptainer. A long-lived contained runner
polls for review work, runs the API-bound session, and posts. It is a
**pull-model** daemon — it reaches out for work, nothing reaches in — so it
lives behind the Torch cluster's no-inbound-SSH / 2FA posture like every other
lab process and opens no new access surface.

What it buys:

- **Our own rate limits.** No GitHub-minute meter, and no per-round
  unauthenticated clone — a warm agent install kills the 429 class outright.
- **Co-location with the tick.** Same host, same throttle scheduler, no
  handoff to GitHub-hosted infra and back.

The cost is a caveat that must be taken seriously, because it is the one thing
the ephemeral hosted runner gives for free:

> **Secrets isolation.** cluster0 holds the reviewer keys, and the PR HEAD
> under review is untrusted same-repo code (`reviewer-infra.md`'s threat
> model). The containment must be airtight: `--containall` so the session
> cannot reach the host filesystem or `/proc`, a **sanitized checkout** that
> strips any `CLAUDE.md`/hooks the untrusted tree ships (the
> instruction-smuggling class), a read-only tree, and a **per-session token
> scoped to just the post** — never the daemon's standing keys. The reviewer
> already runs with no write credential in its session (the tokenless split);
> the added exposure is only the standing keys the daemon holds, which the
> scoped token is what removes.

This is the same jail the dispatcher already needs for eval jobs on untrusted
trees, so B reuses containment we build anyway rather than inventing its own.

## What stays on GitHub-hosted regardless

The lightweight required CI (`ci` — lint, types, test, lock, gitleaks). It is
the *required* check, it is cheap, it runs on every PR from every author, and
ephemeral hosted runners are its right home. Do not conflate it with the
advisory review: CI gates merges and must stay boring and hosted; the review is
advisory and is the only thing whose placement is in question.

## Near term: live-testing on cluster0

While the advisory path is down and being fragile, do not gate progress on
PR-review convergence. The dispatcher and climb work (park/wake,
`DispatchedMeasurer`) is validated best by running it **live on cluster0**
against a real benchmark, not by waiting for review rounds to converge. That
exercise also builds out the exact containment placement B would reuse
(`--containall`, sanitized checkout, scoped token), so the live-testing path
and the eventual review-daemon path share their hardest piece.

## Recommendation

1. **Now.** Keep CI on GitHub-hosted (required, cheap, hosted). Treat the
   advisory review as best-effort: give it a cooldown, stop re-triggering under
   load, and never block a merge on it (it is not a required check).
2. **Now.** Pivot dispatcher/climb validation to live testing on cluster0
   rather than PR-review convergence.
3. **When review is worth hardening.** Build placement B — the contained review
   daemon on cluster0 — reusing the dispatcher's containment. This removes the
   fragility force (warm install, our own limits) directly.
4. **At the public flip.** Re-decide. Hosted minutes become free, so the cost
   force disappears; only the fragility-and-control win would still justify B.
   If the flakiness has been tamed another way by then, staying fully hosted
   (A) is the simpler end state.
