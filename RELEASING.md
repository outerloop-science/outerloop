# Releasing

## Versioning

- SemVer 0.x; single source `src/outerloop/__init__.py`; tags `vX.Y.Z`;
  Keep-a-Changelog. Pre-releases use PEP 440 suffixes (`0.1.0.dev0`,
  `0.1.0rc1`) and are tagged the same way (`v0.1.0.dev0`).

## State that outlives a release

Some state is written by one kernel version and read by the next: run
records and their stage keys, PR branches and the publish journal, snapshot
and line refs, the research-log ledger and its retry intents, inbox messages
and their deduplication keys, syscall staging files, measurement caches, the
launch journal, PR-body and comment markers. A PR that changes how any of
these is written or read includes four things:

1. A compatibility statement in the PR body: which surfaces change, the
   oldest state still read, what happens to runs and PRs in flight on the
   first tick after the upgrade, and whether rolling back is safe.
2. One fixture produced by the previous release (a run record, an inbox, a
   branch layout), exercised through the new code: first pass, a second
   idempotent pass, and a retry after an interruption.
3. A backfill or an explicit tolerance for the old state, including missing
   fields and ended runs. If a case is not supported, the PR says so instead
   of leaving it to the operator to discover.
4. One `Upgrading:` line in the changelog: "no action needed; the first tick
   does X", or the exact operator command. Automatic migration is claimed
   only when the fixture proves it.

Text an agent has already received is state too. Changing a message's
wording does not reach a parked run unless its deduplication key changes.

## Cutting a release

1. Before the version PR, list the merged PRs since the last tag that touch
   the state above and check each has the four items; run their fixtures
   once more against the release candidate, covering an open, a merged and
   an ended legacy run. Collect the `Upgrading:` lines into one section at
   the top of the release's changelog entry.
2. PR: bump `__version__`, set `CITATION.cff`'s `version` and `date-released`,
   and move the `[Unreleased]` entries under the new version. A dev or rc pre-release still bumps the version (PyPI never
   accepts a version twice, so the next one is `.dev1`, `rc2`, ...) but leaves
   `[Unreleased]` in place until the final release.
3. `git tag vX.Y.Z && git push origin vX.Y.Z`. The `release` workflow builds
   and publishes `outerloop-science` to PyPI through Trusted Publishing; the
   one-time PyPI setup is described at the top of
   `.github/workflows/release.yml`.
4. Once the `release` workflow run for the tag is green (`gh run watch` on
   it), `pip install outerloop-science==X.Y.Z` in a fresh venv, then
   `outerloop --help`. This comes before the GitHub release: publishing it is
   what Discord announces, so nothing is announced that did not install.
5. `gh release create vX.Y.Z --generate-notes`, with `--prerelease` for a dev
   or rc tag.
6. Announce. Discord's `#announcements` gets the release from the GitHub
   webhook on its own (docs/community.md). For a final release, also write
   the post for X (`@outerloop_sci`) and Bluesky (`@outerloop.science`): one
   or two sentences on what changed for the reader, the install line, the
   link to the release. Dev and rc pre-releases are not posted to X or
   Bluesky; Discord gets them through the webhook like any release.

## Public repo

Public since 2026-09-05. Secret scanning and push protection are on, and
`main` is protected by `scripts/setup_branch_protection.sh` (pull request
required, the `ci` check, conversations resolved, no force-push, admins
included). History is immutable now; prevention (gitleaks in pre-commit and
CI, push protection) is the real defense. `CITATION.cff` is still owed.
