"""The maintenance scan: a read-only agent session over a checkout of a
repository's default branch that records cleanup, upgrade, test-health and
performance items as findings, and the digest those findings render into.

It reuses the reviewer's machinery — the FINDINGS_SCHEMA verdict through the
syscall tool, lenses fanned out and merged by the summarizer, the emit/post
split — and differs in three places: the brief scans a tree instead of a
diff, nothing is blocking, and the destination is one rolling issue
(docs/design/reviewer-infra.md, "Maintenance scan"). Any repository can run
it from the reusable workflow; the brief assumes nothing about this one."""

from __future__ import annotations

import contextlib
import logging
import urllib.parse
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from outerloop.harness import Harness, backend_id
from outerloop.markers import marker
from outerloop.posting import EXPECTED_FAILURES
from outerloop.review import DEFAULT_SYSCALL_CMD, Finding, ReviewResult, sanitize
from outerloop.review_agent import emit_envelope
from outerloop.role_runner import run_role
from outerloop.rolespec import RoleSpec

log = logging.getLogger(__name__)

MARKER = marker("maintenance-digest")
DIGEST_TITLE = "Maintainer digest"
ADVISORY = (
    "*Advisory findings from `outerloop`. The maintainer decides: items marked "
    "**Decision** need a call before any change; the rest are mechanical and may "
    "be taken as work orders. The scan edits nothing.*"
)

# Each lens is one section of the digest; `general` is the whole checklist.
# The library lives here; which lenses run is the caller workflow's matrix.
MAINTENANCE_LENSES: dict[str, str] = {
    "pathways": (
        "LENS — dead and unused pathways: symbols, CLI flags, config keys and "
        "environment knobs with no caller or no documentation; compatibility "
        "shims and what each still guards; test-only code living in the "
        "package. Grep across source, tests, scripts and docs before calling "
        "anything unused."
    ),
    "duplication": (
        "LENS — logic with more than one owner: the same rule implemented in "
        "two places (two parsers of one file format, two copies of one "
        "sequence), private helpers imported across modules, argument groups "
        "or fixtures copied between entry points or test files, version pins "
        "repeated in several files."
    ),
    "structure": (
        "LENS — size and shape: the largest modules and longest functions "
        "(measure them), import cycles and the in-function imports that hide "
        "them, templates or data embedded in code, and the natural seams a "
        "split would follow."
    ),
    "upgrades": (
        "LENS — dependencies and tooling: pinned versions against the latest "
        "available (the package index, GitHub releases), CI action versions, "
        "runner images, linter and type-checker settings that could be "
        "tightened cheaply, interpreter versions exercised. Name the pin and "
        "the current upstream for each."
    ),
    "tests": (
        "LENS — test-suite health: the slowest tests and why, real sleeps and "
        "real subprocesses where a fake would do, fixtures and fakes defined "
        "several times, tests that no longer pin the behavior they name, "
        "markers declared but unused."
    ),
    "performance": (
        "LENS — repeated work on the hot path: find the loop or entry point "
        "the repository runs most often and count what it re-reads, re-lists "
        "or re-fetches per iteration and per record; caching that is missing, "
        "network calls without conditional requests, files parsed more than "
        "once."
    ),
    "docs": (
        "LENS — documentation drift: comments that narrate history instead of "
        "intent, changelog sections to consolidate, roadmap or design notes "
        "whose status no longer matches the code, knobs and flags the docs "
        "never name, wording that disagrees between two documents."
    ),
    "architecture": (
        "LENS — abstraction and extensibility, forward-looking rather than "
        "cleanup: where two abstractions could be unified or a layer dropped "
        "so the system is simpler to reason about; and, holding the principle "
        "that a new backend, benchmark, or role should need zero kernel "
        "change, where an extension point is missing so adding one today "
        "forces a kernel edit. Name the files and propose the merge or the "
        "seam; mark these decisions — a refactor of an abstraction many "
        "parts of the system depend on is the maintainer's call, not a "
        "mechanical change."
    ),
}

SYSTEM_PROMPT = (
    "You are the maintainer's periodic scan of this repository. You read the whole "
    "tree, measure rather than guess, and record each item worth doing as a finding. "
    "Nothing you find blocks anything: the maintainer reads the digest and decides.\n\n"
    "What to record: cleanup, simplification, upgrade, test-health and performance "
    "items — what a careful maintainer would put on their own list after a week away. "
    "Skip style nits a linter already reports and work the repository's own roadmap "
    "already tracks as planned.\n\n"
    "Evidence: every item names a file and a line, and a measured fact where one "
    "exists (a line count, a call count, the pinned and the latest version, a test "
    "duration). Say what you ran.\n\n"
    "Shape of each finding:\n"
    "- --kind change: mechanical, safe for an agent to do in a pull request without a "
    "design call.\n"
    "- --kind question: needs the maintainer's decision first (when to drop a "
    "compatibility path, whether to re-verify a pinned tool, which module owns a "
    "duplicated rule).\n"
    "- --kind note: worth knowing, not worth a change.\n"
    "- --category: the digest section the item belongs to, one of {sections}.\n"
    '- --detail: start with effort and risk, for example "S, low." (S is under an '
    "hour, M an afternoon, L a day or more; risk is what could break), then the "
    "evidence.\n"
    "- never --blocking.\n\n"
    "Your concluding notes open the digest: one line on what is healthy, then the "
    "three items most worth doing, one sentence each."
)


def _investigation(ref: str, syscall_cmd: str) -> str:
    return (
        f"The repository is checked out in your working directory at commit {ref}. "
        "Use Read, Grep and Glob, and run read-only commands in the shell: line "
        "counts, the test suite with durations, the package manager's outdated "
        "list, the linter's statistics. Do not modify the tree, do not install "
        "anything beyond what its own lockfile describes, and do not push or "
        "post anything — your only product is the verdict.\n\n"
        "Record each item as you confirm it, one command per item:\n"
        f"  {syscall_cmd} finding --file <path> [--line N] "
        "--confidence <low|medium|high> --category <section> --summary <one line> "
        "--detail <effort, risk, then the evidence> --kind <change|question|note>\n"
        "When you are done, commit your verdict and end your turn:\n"
        f"  {syscall_cmd} conclude --notes <what is healthy; the three items most worth doing>\n"
        "The verdict you commit is your final answer — do not also restate it in a message."
    )


def build_maintenance_brief(
    repo: str,
    ref: str,
    today: str | None = None,
    *,
    syscall_cmd: str = DEFAULT_SYSCALL_CMD,
    lens: str = "",
) -> str:
    """The scan brief: the standing prompt, the lens (or every section for
    `general`), the investigation instruction and the repository line. An
    unknown lens fails loudly, as the reviewer's does."""
    if lens and lens != "general" and lens not in MAINTENANCE_LENSES:
        raise ValueError(f"unknown maintenance lens {lens!r} (have: {sorted(MAINTENANCE_LENSES)})")
    sections = ", ".join(MAINTENANCE_LENSES)
    if lens and lens != "general":
        focus = MAINTENANCE_LENSES[lens]
    else:
        focus = "Cover every section:\n\n" + "\n\n".join(MAINTENANCE_LENSES.values())
    header = f"Today's date: {today}\n" if today else ""
    header += f"Repository: {repo} at {ref}"
    return (
        f"{SYSTEM_PROMPT.format(sections=sections)}\n\n{focus}\n\n"
        f"{_investigation(ref, syscall_cmd)}\n\n{header}\n"
    )


_KIND_ORDER = {"question": 0, "change": 1, "suggestion": 1, "note": 2}
_CONFIDENCE_ORDER = {"high": 0, "medium": 1, "low": 2}
_LABEL = {"question": "**Decision.** ", "note": "*Note.* "}


def _item(finding: Finding, repo: str, ref: str) -> str:
    # backticks stripped: a file value containing one would close the code
    # span and render model markdown inline (same rule as the review body)
    safe_file = finding.file.replace("`", "")
    where = f"`{safe_file}`" + (f":{finding.line}" if finding.line else "")
    link = (
        f"https://github.com/{repo}/blob/{urllib.parse.quote(ref)}/{urllib.parse.quote(safe_file)}"
    )
    if finding.line:
        link += f"#L{finding.line}"
    summary = finding.summary.rstrip(".!?…")
    if summary.count("`") % 2:
        summary += "`"
    detail = finding.detail + ("`" if finding.detail.count("`") % 2 else "")
    label = _LABEL.get(finding.kind, "")
    return f"- {label}**{summary}.** {detail} ([{where}]({link}); {finding.confidence})"


def digest_title(today: str) -> str:
    """The rolling issue's title, carrying the scan date so its freshness shows
    in the issue list without opening it. Refreshed on every scan."""
    return f"{DIGEST_TITLE} — {today}"


def render_digest(
    result: ReviewResult,
    *,
    repo: str,
    ref: str,
    today: str,
    reviewed_by: str,
) -> str:
    """The rolling issue's body: marker first, the header and the advisory
    line, the counts, the scan's own summary, then one section per category
    with decisions first. Every string in `result` is already sanitized by
    `result_from_data`; the marker leads so the poster can find the issue."""
    findings = result.findings
    decisions = sum(1 for f in findings if f.kind == "question")
    notes = sum(1 for f in findings if f.kind == "note")
    mechanical = len(findings) - decisions - notes
    who = sanitize(reviewed_by, 120) or "unattributed"
    lines = [
        MARKER,
        f"**{DIGEST_TITLE}** — {repo} at `{ref[:8]}` on {today}; scanned by `{who}`.",
        "",
        ADVISORY,
        "",
        f"{len(findings)} items: {decisions} need a decision, {mechanical} are "
        f"mechanical, {notes} are notes.",
        "",
    ]
    if result.notes:
        # keep the top a short summary (title, advisory, counts); the scan's
        # own verdict and its rejected-findings reasoning fold away below it
        lines += [
            "<details><summary>Scan verdict and rejected findings</summary>",
            "",
            result.notes,
            "",
            "</details>",
            "",
        ]
    by_section: dict[str, list[Finding]] = {}
    for f in findings:
        section = f.category if f.category in MAINTENANCE_LENSES else "other"
        by_section.setdefault(section, []).append(f)
    for section in [*MAINTENANCE_LENSES, "other"]:
        items = by_section.get(section)
        if not items:
            continue
        items.sort(key=lambda f: (_KIND_ORDER.get(f.kind, 2), _CONFIDENCE_ORDER[f.confidence]))
        lines += [f"### {section}", ""]
        lines += [_item(f, repo, ref) for f in items]
        lines.append("")
    lines.append(
        "_Each scan replaces this body; earlier digests are in the edit history. "
        "Run a scan by hand from the Actions tab (maintenance → Run workflow)._"
    )
    return "\n".join(lines).rstrip() + "\n"


def render_stub(detail: str, *, repo: str, ref: str, today: str, who: str) -> str:
    """What the poster writes when the scan could not run: the reason, on the
    digest issue, never silence."""
    reason = sanitize(detail, 300)
    by = f" ({sanitize(who, 120)})" if who else ""
    return (
        f"{MARKER}\n**{DIGEST_TITLE}** — the scan of {repo} at `{ref[:8]}` on {today} "
        f"could not run{by}: {reason}"
    )


def run_maintenance_scan(
    repo: str,
    ref: str,
    harness: Harness,
    workspace: Path,
    *,
    spec: RoleSpec | None = None,
    emit_path: Path,
    today: str | None = None,
    lens: str = "",
) -> str | None:
    """One lens session over `workspace` (a default-branch checkout the caller
    prepared and sanitized). EVERY outcome writes an envelope for the posting
    job — findings, or a skip-stub naming why — so a missing artifact always
    means a broken session. Returns "emitted", or None when it could not
    produce a verdict. Advisory: never raises the expected failures."""
    from outerloop.roles import maintainer_spec

    spec = spec or maintainer_spec()
    today = today or datetime.now(UTC).date().isoformat()
    try:
        from outerloop.syscall import tool_command

        brief = build_maintenance_brief(
            repo, ref, today, syscall_cmd=tool_command(workspace), lens=lens
        )
        role_result = run_role(spec, harness, brief, workspace)
        if not role_result.ok or role_result.data is None:
            detail = role_result.error or role_result.session.stop_reason
            log.warning("maintenance scan produced no verdict on %s (%s): %s", repo, lens, detail)
            emit_envelope(
                emit_path,
                repo,
                0,
                kind="skip-stub",
                detail=detail,
                reviewed_by=backend_id(harness),
                lens=lens,
            )
            return None
        emit_envelope(
            emit_path,
            repo,
            0,
            kind="findings",
            data=role_result.data,
            reviewed_by=backend_id(harness),
            lens=lens,
        )
        cost = role_result.session.cost_usd
        log.info(
            "emitted maintenance findings for %s (%s; cost=%s turns=%d)",
            repo,
            lens or "general",
            f"${cost:.2f}" if cost else "unreported",
            role_result.session.num_turns,
        )
        return "emitted"
    except EXPECTED_FAILURES as exc:  # advisory: never red the repository's Actions
        log.warning("maintenance scan did not complete: %s: %s", type(exc).__name__, exc)
        with contextlib.suppress(Exception):
            emit_envelope(
                emit_path,
                repo,
                0,
                kind="skip-stub",
                detail=f"{type(exc).__name__}: {exc}",
                reviewed_by=backend_id(harness),
                lens=lens,
            )
        return None


def lens_names(lenses: Iterable[str]) -> list[str]:
    """The lens names a caller configured, `general` included, unknown ones
    refused — so a misspelled matrix entry fails at configuration time."""
    out = []
    for name in lenses:
        if name != "general" and name not in MAINTENANCE_LENSES:
            raise ValueError(f"unknown maintenance lens {name!r}")
        out.append(name)
    return out
