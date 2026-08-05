# Releasing

## Versioning

- SemVer 0.x; single source `src/autoresearch/__init__.py`; tags `vX.Y.Z`;
  Keep-a-Changelog.

## Cutting a release

1. PR: bump `__version__`, move `[Unreleased]` entries under the new version.
2. `git tag vX.Y.Z && git push origin vX.Y.Z`, then `gh release create`.

## If this repo goes public

Plausible with a code white paper. Before flipping:

1. Freeze merges; move any non-releasable branches to a private mirror first —
   the flip publishes every branch and tag.
2. Full-history audit on a fresh mirror clone: `gitleaks git .`, large-object
   scan, manual greps for tokens/netids/home paths. This repo handles bot and
   API credentials — the audit is load-bearing.
3. Editorial pass: remove the private banner, internal links, target-repo
   specifics that aren't public yet.
4. Add `CITATION.cff`; cut the release; `gh repo edit --visibility public`.
5. Post-flip: enable secret scanning + push protection; re-run
   `scripts/setup_branch_protection.sh`.

History is immutable after the flip; prevention (gitleaks in pre-commit + CI) is
the real defense.
