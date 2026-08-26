"""Merge k lens opinions into one review round (the wide first round,
docs/design/reviewer-infra.md).

Runs in the split topology's read-only session job: inputs are the lens
sessions' emitted envelopes (downloaded artifacts), output is ONE envelope for
`review_post_cli`. Pure passthrough when only one real opinion exists; a
model session (the summarizer role) only when there is actual merging to do.
Every skip emits a stub — a silent summarizer would read as a quiet clean day
(the same lesson as the reviewer split).

Env: PR_REPO, PR_NUMBER, SUMMARIZE_DIR (a directory holding the downloaded
`findings.json` envelopes, possibly in subdirectories), REVIEW_EMIT_FILE
(the merged envelope's path), plus the reviewer backend contract
(REVIEW_BACKEND / REVIEW_MODEL / REVIEW_HERMES_* / the key vars) exactly as
`review_agent_cli` reads it.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from pathlib import Path

from autoresearch.review_agent import _emit, backend_id
from autoresearch.role_runner import run_role
from autoresearch.roles import summarizer_spec

log = logging.getLogger(__name__)

MAX_OPINIONS = 8  # artifacts are workflow-authored, but cap the read anyway


def _load_envelopes(root: Path, repo: str, number: int) -> list[dict]:
    """Every valid envelope under `root` that names THIS PR. An envelope
    naming a different PR is refused (same rule as the poster: artifacts
    cross a job boundary, so nothing in them is trusted)."""
    out: list[dict] = []
    for path in sorted(root.glob("**/findings.json"))[:MAX_OPINIONS]:
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("unreadable envelope %s: %s", path, exc)
            continue
        if not isinstance(data, dict):
            continue
        if data.get("repo") != repo or data.get("number") != number:
            log.warning("envelope %s names a different PR; refused", path)
            continue
        out.append(data)
    return out


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    repo = os.environ.get("PR_REPO", "").strip()
    number_raw = os.environ.get("PR_NUMBER", "").strip()
    emit_env = os.environ.get("REVIEW_EMIT_FILE", "").strip()
    src = os.environ.get("SUMMARIZE_DIR", "").strip()
    if not repo or not number_raw.isdigit() or not emit_env or not src:
        log.warning("PR_REPO/PR_NUMBER/REVIEW_EMIT_FILE/SUMMARIZE_DIR unset; skipping")
        return 0
    number = int(number_raw)
    emit_path = Path(emit_env).resolve()

    def stub(detail: str) -> int:
        _emit(emit_path, repo, number, kind="skip-stub", detail=detail, reviewed_by="summarizer")
        return 0

    envelopes = _load_envelopes(Path(src).resolve(), repo, number)
    if not envelopes:
        return stub("no lens envelopes found (all lens sessions died before emitting)")
    reals = [
        e for e in envelopes if e.get("kind") == "findings" and isinstance(e.get("data"), dict)
    ]
    if not reals:
        details = "; ".join(
            f"{e.get('lens') or e.get('reviewed_by') or '?'}: {e.get('detail') or e.get('kind')}"
            for e in envelopes
        )
        # all skipped/failed: ONE stub summarizing why (clean skips stay
        # clean — the poster's own skip re-check silences bot/opt-out PRs)
        if all(e.get("kind") == "skip-clean" for e in envelopes):
            _emit(emit_path, repo, number, kind="skip-clean", detail=details)
            return 0
        return stub(f"no lens produced findings ({details})")
    failed = [e for e in envelopes if e.get("kind") == "skip-stub"]
    if len(reals) == 1:
        # nothing to merge: pass the one real opinion through — but FAILED
        # sibling lenses must still reach the posted round (a lone success
        # must not hide that most of the panel died)
        only = reals[0]
        data = dict(only.get("data") or {})
        if failed:
            lost = "; ".join(
                f"{e.get('lens') or e.get('reviewed_by') or '?'}: "
                f"{str(e.get('detail') or '')[:120]}"
                for e in failed
            )
            notes = str(data.get("notes") or "")
            data["notes"] = (notes + "\n\n" if notes else "") + (
                f"[panel] lens sessions that did NOT run this round: {lost}"
            )
        _emit(
            emit_path,
            repo,
            number,
            kind="findings",
            data=data,
            reviewed_by=str(only.get("reviewed_by", "")),
            lens=str(only.get("lens", "")),
        )
        log.info("single real opinion (%s); passed through", only.get("lens") or "unlabeled")
        return 0

    from autoresearch.review import build_summarizer_brief
    from autoresearch.review_agent_cli import resolve_reviewer_harness
    from autoresearch.syscall import tool_command

    spec = summarizer_spec()
    harness, why, _backend = resolve_reviewer_harness(spec)
    if harness is None:
        return stub(f"summarizer harness unavailable: {why}")
    with tempfile.TemporaryDirectory(prefix="summarize-") as tmp:
        workspace = Path(tmp)
        brief = build_summarizer_brief(reals, syscall_cmd=tool_command(workspace))
        role_result = run_role(spec, harness, brief, workspace)
    if not role_result.ok or role_result.data is None:
        detail = role_result.error or role_result.session.stop_reason
        return stub(f"summarizer session produced no verdict: {detail}")
    lenses = "+".join(str(e.get("lens") or "?") for e in reals)
    _emit(
        emit_path,
        repo,
        number,
        kind="findings",
        data=role_result.data,
        reviewed_by=f"summarizer:{backend_id(harness)} over {lenses}",
    )
    log.info("merged %d opinions (%s)", len(reals), lenses)
    return 0


if __name__ == "__main__":
    sys.exit(main())
