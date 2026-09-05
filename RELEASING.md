# Releasing

## Versioning

- SemVer 0.x; single source `src/outerloop/__init__.py`; tags `vX.Y.Z`;
  Keep-a-Changelog. Pre-releases use PEP 440 suffixes (`0.1.0.dev0`,
  `0.1.0rc1`) and are tagged the same way (`v0.1.0.dev0`).

## Cutting a release

1. PR: bump `__version__` and move the `[Unreleased]` entries under the new
   version. A dev or rc pre-release skips both; `[Unreleased]` stays.
2. `git tag vX.Y.Z && git push origin vX.Y.Z`. The `release` workflow builds
   and publishes `outerloop-science` to PyPI through Trusted Publishing; the
   one-time PyPI setup is described at the top of
   `.github/workflows/release.yml`.
3. `gh release create vX.Y.Z --generate-notes`, with `--prerelease` for a dev
   or rc tag.
4. `pip install outerloop-science==X.Y.Z` in a fresh venv, then `outerloop --help`.

## Public repo

Public since 2026-09-05. Secret scanning and push protection are on, and
`main` is protected by `scripts/setup_branch_protection.sh` (pull request
required, the `ci` check, conversations resolved, no force-push, admins
included). History is immutable now; prevention (gitleaks in pre-commit and
CI, push protection) is the real defense. `CITATION.cff` is still owed.
