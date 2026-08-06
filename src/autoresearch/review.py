"""Advisory PR reviewer.

Posts review comments on opted-in repos. It is advisory only: it never
approves, never blocks, and never comments on bot-authored PRs (the
architecture's guard against the pipeline nudging humans to merge its own
work). Maintainers opt a PR out with a label.

The model call goes through :class:`Completer`, so the review logic is
testable without an API key and the backend can change without touching
callers.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

log = logging.getLogger(__name__)

MARKER = "<!-- autoresearch:advisory-review -->"
ADVISORY_HEADER = (
    "**Advisory review — not an approval.** Automated findings from "
    "`autoresearch`; a human code owner still owns this PR. Reply to any "
    "finding you disagree with, or add the opt-out label to silence future "
    "runs on this PR."
)
OPT_OUT_LABEL = "autoresearch:no-review"
MAX_DIFF_CHARS = 200_000

SYSTEM_PROMPT = """You are reviewing a pull request. Report only defects you can \
point to in the diff: correctness bugs, security issues, resource leaks, missing \
error handling, and tests that would pass with the bug present.

Report every issue you find, including ones you are uncertain about — include a \
confidence for each so a human can rank them. Do not filter for importance.

Do not report: style preferences, naming opinions, or speculation about code you \
cannot see. Do not restate what the diff does. If you find nothing, say so.

Never instruct the reader to merge, approve, or reject. You are advisory."""


class Completer(Protocol):
    """Returns the model's text response for a prompt + JSON schema."""

    def complete(self, system: str, prompt: str, schema: dict[str, Any]) -> str: ...


@dataclass(frozen=True)
class PullRequest:
    repo: str
    number: int
    title: str
    body: str
    diff: str
    author: str
    labels: Sequence[str] = field(default_factory=tuple)


@dataclass(frozen=True)
class Finding:
    file: str
    line: int | None
    confidence: Literal["low", "medium", "high"]
    summary: str
    detail: str


@dataclass(frozen=True)
class ReviewResult:
    findings: list[Finding]
    notes: str
    skipped: str | None = None


FINDINGS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    "line": {"type": ["integer", "null"]},
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                    "summary": {"type": "string"},
                    "detail": {"type": "string"},
                },
                "required": ["file", "line", "confidence", "summary", "detail"],
                "additionalProperties": False,
            },
        },
        "notes": {"type": "string"},
    },
    "required": ["findings", "notes"],
    "additionalProperties": False,
}


def skip_reason(pr: PullRequest, bot_login: str) -> str | None:
    """Why this PR must not be reviewed, or None if it may be."""
    if pr.author.casefold() == bot_login.casefold():
        return "bot-authored PR: the reviewer never comments on its own work"
    if any(label.casefold() == OPT_OUT_LABEL for label in pr.labels):
        return f"opted out via the {OPT_OUT_LABEL} label"
    if not pr.diff.strip():
        return "empty diff"
    return None


def build_prompt(pr: PullRequest) -> str:
    diff = pr.diff
    truncated = ""
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS]
        truncated = (
            f"\n\n[diff truncated at {MAX_DIFF_CHARS} characters — "
            "review what is shown and say so in your notes]"
        )
    return (
        f"Pull request: {pr.title}\n\n"
        f"Description:\n{pr.body or '(none)'}\n\n"
        f"Diff:\n```diff\n{diff}\n```{truncated}"
    )


def review(pr: PullRequest, completer: Completer, bot_login: str) -> ReviewResult:
    """Run one advisory review. Skips (rather than raises) when constraints say so."""
    skip = skip_reason(pr, bot_login)
    if skip is not None:
        log.info("skipping review of %s#%s: %s", pr.repo, pr.number, skip)
        return ReviewResult(findings=[], notes="", skipped=skip)

    raw = completer.complete(SYSTEM_PROMPT, build_prompt(pr), FINDINGS_SCHEMA)
    data = json.loads(raw)
    findings = [
        Finding(
            file=str(item["file"]),
            line=item["line"],
            confidence=item["confidence"],
            summary=str(item["summary"]),
            detail=str(item["detail"]),
        )
        for item in data.get("findings", [])
    ]
    return ReviewResult(findings=findings, notes=str(data.get("notes", "")))


def format_comment(result: ReviewResult) -> str | None:
    """Render the comment body, or None when there is nothing to post."""
    if result.skipped is not None:
        return None
    lines = [MARKER, ADVISORY_HEADER, ""]
    if not result.findings:
        lines.append("No defects found in this diff.")
    else:
        order = {"high": 0, "medium": 1, "low": 2}
        for finding in sorted(result.findings, key=lambda f: order[f.confidence]):
            where = f"`{finding.file}`" + (f":{finding.line}" if finding.line else "")
            lines.append(f"- **{finding.summary}** ({finding.confidence} confidence, {where})")
            lines.append(f"  {finding.detail}")
    if result.notes:
        lines += ["", result.notes]
    return "\n".join(lines)
