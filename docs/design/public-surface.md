# The public surface: threat model for open target repos

**Status: v1 (2026-08-08). The audit below GATES any repo carrying our
workflows going public, alongside the verifier. Written for self-hosters as
much as for us: if you attach autoresearch to a public repo, this is your
attack surface.**

## The scenario

A target repo goes public. Strangers can now open issues, comment on PRs,
open fork PRs, and (with any grant of triage) apply labels. Several of those
inputs feed systems that hold credentials or submit cluster jobs. The
headline threat: **a public interaction that ends with code execution on the
operator's compute, or with a secret exfiltrated.**

## What already holds (defenses in place)

- **Author-association gating**: issue intake and PR-comment wakes qualify
  only OWNER/MEMBER/COLLABORATOR authors; label-based qualification demands
  provenance (the labeler's permission verified via timeline events, not
  label presence). A stranger's issue or comment reaches no session today.
- **Untrusted text is data**: everything ingested from GitHub (issues,
  comments, reports) is fenced with computed fences and marked as data, not
  instructions, in briefs and wake prompts.
- **Sessions are contained**: Apptainer `--containall`, scrubbed env (no
  PAT, no billing keys), per-run HOME; transcripts are key-redacted and
  stored outside any target clone. (A full secret-scan pass before storage
  is still open roadmap work — it does NOT count toward this audit yet.)
- **Claims are orchestrator-measured**: nothing a session asserts is
  trusted; baselines and candidates are re-measured by the orchestrator, and
  target CI re-verifies on GitHub's runners.
- **The bot cannot write to this repo** (account-level), and the orchestrator
  never executes code from PRs it did not author (see invariant below).

## The gap that gates the flip: `pull_request_target`

The advisory reviewer runs on `pull_request_target` so it can hold secrets
(per-repo `ANTHROPIC_REVIEWER_KEY`; optionally a checkout key). On a public
repo that
trigger fires for **fork PRs from strangers** — the classic pwn-request
shape: privileged context + attacker-influenced event.

Current mitigations already in the reusable workflow
(`advisory-review-agent.yml`): the fork gate
(`head.repo.full_name == github.repository`) sits before any step, so no
fork PR ever reaches the session. The workflow checks out the PR head
(`persist-credentials: false`) and the judge runs with a shell over it — so a
prompt-injected PR could get the judge to EXECUTE its checked-out code. That is
not the boundary; the boundary is that the session is disposable and holds
nothing worth taking for a WRITE: a scrubbed environment and no write token
(the tokenless split keeps that in a separate post job). The credentials that
ARE in the session — and a shell judge can `/proc`-read them — are its own
spend-capped, role-isolated model API key and a READ-scoped `GITHUB_TOKEN`
(and, while the reviewer repo is private, a read-only deploy key; see
reviewer-infra.md). None of those can write to the repo. So the residual
exposure of a shell judge on a bare runner is capped spend plus reading what a
read-scoped token already reads, plus whatever it can egress from that
ephemeral runner — accepted for an advisory role (the container/cluster path is
the tighter option), and the read token drops to zero at the public flip. Fork-PR
review remains out of scope until it gets its own design.

**Audit checklist before any public flip** (each item verified on the live
workflow files of the repo being flipped, not on memory of them):

1. The fork gate is present, at the JOB level, on every caller workflow —
   and its polarity is "same repo only", not an allowlist that can drift.
2. The judge session runs a shell over the PR-head checkout, so it CAN be
   induced to execute PR-head code — that is acceptable ONLY because item 1
   restricts the head to the SAME repo (fork code never reaches it) and the
   session job is disposable and holds no write token (item 3). Verify those
   hold; do NOT rely on "PR code is never executed" — it is, and the boundary
   is the runner + the token split, not the tool set. (The post job, which
   does hold the write token, runs NO session and touches nothing from the head.)
3. The token split holds: the JOB that runs the session has at most
   `pull-requests: read` (its shell judge can `/proc`-read whatever is in its
   env, so no write token may be there); the separate post JOB holds
   `pull-requests: write` and runs no session. Verify per-job `permissions:`,
   not just top-level.
4. Labels that trigger privileged runs (`autoresearch:review`): GitHub
   allows label application at TRIAGE, not write — so GitHub's own
   permission model is NOT sufficient gating on a public repo with triage
   grants. Verify the code-side provenance check (labeler permission via
   timeline events) covers every label-triggered privileged path, and that
   no automation applies labels on behalf of unprivileged users.
5. Prompt-injection resistance re-checked. With the fork gate on, the
   reviewer sees only same-repo PRs — but same-repo authors can be
   compromised accounts, PR bodies quote text from anyone's issues, and a
   self-hoster may deliberately relax the gate for label-approved fork
   PRs. So the output sanitization (approval-language redaction, marker
   stripping, length caps) must hold regardless of gate configuration: a
   hostile diff must not be able to forge an approval or smuggle
   instructions into the comment a human reads.
6. Secrets inventory: which workflows can see which secrets, and is each
   scoped to the least repo set (org-level secrets with repo selection, not
   org-wide).

## Invariants to keep enforced in code (not convention)

- **External code never executes on operator hardware.** The orchestrator
  clones and measures only trees its own bot authored; evaluation of
  stranger contributions belongs to the target's own CI on GitHub-hosted
  runners. (Worth an explicit assertion at the clone/measure boundary:
  refuse a workspace whose head commit is not bot-authored.)
- **Public input can request, never command.** The requested lane's standing
  gate stays association-based on public repos; a public "issue mode" (if
  ever wanted) would be a maintainer-approved-label-only lane, and the label
  provenance check already exists.
- **The contract is read from the default branch only** — never from a PR —
  so a fork PR cannot present a doctored contract to any part of the system.

## Release-process changes

- RELEASING.md's go-public checklist gains a **hostile-interaction section**:
  run this audit, then enable "limit to users with write access" style
  interaction limits for an initial window, and document the incident path
  (revoke org secret, pause tick chain via sentinel, rotate PAT).
- New-repo onboarding docs point here: attaching the reviewer to a public
  repo inherits this threat model on day one.

## Out of scope (for now)

Multi-tenant hosting, GitHub App identity, and consumer-side experiment
runners are design/external.md's territory; nothing here assumes them. The
notebook repo stays permanently private and is unaffected by any target
going public.
