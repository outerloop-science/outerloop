"""Entry point for one maintenance-scan lens session (emit only).

Runs the scan as an agent over a default-branch checkout the workflow prepared
(REVIEW_CHECKOUT) and writes its findings to REVIEW_EMIT_FILE for the posting
job. Exits 0 on every outcome — an advisory scan must never turn a
repository's Actions red.

Env: MAINTAIN_REPO (owner/repo), MAINTAIN_REF (the checked-out commit),
REVIEW_CHECKOUT, REVIEW_EMIT_FILE, REVIEW_LENS; the backend contract is the
reviewer's (REVIEW_BACKEND / REVIEW_MODEL / REVIEW_HERMES_* / the key vars),
resolved by the same code.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from outerloop.maintain import run_maintenance_scan
from outerloop.review_agent import _emit, sanitize_checkout
from outerloop.review_agent_cli import resolve_reviewer_harness
from outerloop.roles import maintainer_spec

log = logging.getLogger(__name__)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    repo = os.environ.get("MAINTAIN_REPO", "").strip()
    ref = os.environ.get("MAINTAIN_REF", "").strip()
    if not repo or not ref:
        log.warning("MAINTAIN_REPO/MAINTAIN_REF unset; skipping")
        return 0
    emit_env = os.environ.get("REVIEW_EMIT_FILE", "").strip()
    lens = os.environ.get("REVIEW_LENS", "").strip()
    backend = os.environ.get("REVIEW_BACKEND", "claude").lower()

    def stub(detail: str) -> int:
        log.warning("%s; skipping scan", detail)
        if emit_env:
            _emit(
                Path(emit_env).resolve(),
                repo,
                0,
                kind="skip-stub",
                detail=detail,
                reviewed_by=backend,
                lens=lens,
            )
        return 0

    if not emit_env:
        return stub("REVIEW_EMIT_FILE is unset (nothing to hand to the posting job)")
    # Fail closed on the tree: defaulting to cwd would scan the kernel's own
    # checkout instead of the repository the workflow prepared.
    checkout = os.environ.get("REVIEW_CHECKOUT", "").strip()
    if not checkout:
        return stub("REVIEW_CHECKOUT is unset (won't scan the wrong tree)")
    workspace = Path(checkout).resolve()
    if not workspace.is_dir():
        return stub(f"REVIEW_CHECKOUT {workspace} is not a directory")
    # instruction-bearing files in the scanned tree are data, never prompts
    renamed, failed = sanitize_checkout(workspace)
    if failed:
        return stub(f"{failed} instruction file(s) could not be neutralized in the checkout")
    if renamed:
        log.info("neutralized %d instruction file(s) in the checkout", renamed)
    spec = maintainer_spec()
    harness, why, _backend = resolve_reviewer_harness(spec)
    if harness is None:
        return stub(why)
    run_maintenance_scan(
        repo, ref, harness, workspace, spec=spec, emit_path=Path(emit_env).resolve(), lens=lens
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
