#!/usr/bin/env python3
"""The judge's verdict tool — the agent-facing surface for reviewer/verifier
verdicts (docs/design/role-cli.md, Phase 2).

Standalone by contract: the kernel installs its source at `.verdict/verdict`
and a judge runs it as a single allow-listed command (judges have no general
Bash — a distinct RoleSpec capability, not `can_execute`). It replaces the
"emit one big JSON message, kernel parses-and-repairs" path (role_runner): each
finding is one validated call, so the verdict is well-formed BY CONSTRUCTION and
the repair loop disappears.

    python .verdict/verdict finding --file solver.py --line 42 --confidence high \\
        --summary "off-by-one" --detail "the loop skips the last index" --blocking
    python .verdict/verdict conclude --notes "one blocking defect; rest is clean"

`finding` stages into `.verdict/findings.json`; `conclude` commits the whole
verdict to `.verdict/verdict.json` (the ABI the kernel reads). Validation here is
for FAST in-session feedback — the kernel re-validates authoritatively
(`verdict.py`), so this tool is convenience, never a trust boundary.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path

DIR = ".verdict"
STAGING = "findings.json"
ABI = "verdict.json"
CONFIDENCES = ("low", "medium", "high")
KINDS = ("change", "suggestion", "question", "note")
MAX_TEXT = 6_000  # per summary/detail/notes/category
MAX_FINDINGS = 200


class ToolError(Exception):
    """A bad invocation: printed to stderr, exit 2, nothing staged."""


def _load(root: Path) -> dict:
    try:
        return json.loads((root / DIR / STAGING).read_text())
    except FileNotFoundError:
        return {"findings": [], "notes": ""}
    except (OSError, json.JSONDecodeError) as exc:
        raise ToolError(f"staged verdict unreadable ({exc}); run `cancel` to reset") from exc


def _save(root: Path, data: dict) -> None:
    d = root / DIR
    d.mkdir(exist_ok=True)
    (d / STAGING).write_text(json.dumps(data, indent=2))


def cmd_finding(root: Path, args: argparse.Namespace) -> str:
    if not args.file.strip():
        raise ToolError("--file must not be empty")
    if args.confidence not in CONFIDENCES:
        raise ToolError(f"--confidence must be one of {CONFIDENCES}")
    if args.kind not in KINDS:
        raise ToolError(f"--kind must be one of {KINDS}")
    if args.line is not None and args.line < 1:
        raise ToolError("--line is 1-indexed; omit it for a non-local finding")
    for label, text in (("--summary", args.summary), ("--detail", args.detail)):
        if not text.strip():
            raise ToolError(f"{label} must not be empty")
        if len(text) > MAX_TEXT:
            raise ToolError(f"{label} exceeds {MAX_TEXT} chars")
    if args.category and len(args.category) > MAX_TEXT:
        raise ToolError(f"--category exceeds {MAX_TEXT} chars")
    staged = _load(root)
    if len(staged["findings"]) >= MAX_FINDINGS:
        raise ToolError(f"at most {MAX_FINDINGS} findings")
    finding = {
        "file": args.file,
        "line": args.line,  # None when --line omitted: a non-local finding
        "confidence": args.confidence,
        "summary": args.summary,
        "detail": args.detail,
        "blocking": bool(args.blocking),
        "kind": args.kind,
    }
    if args.category:
        finding["category"] = args.category  # verifier gaming-taxonomy; omitted otherwise
    staged["findings"].append(finding)
    _save(root, staged)
    tag = "BLOCKING" if args.blocking else args.kind
    where = f"{args.file}:{args.line or '?'}"
    return f"recorded {tag} finding on {where} ({len(staged['findings'])} so far)."


def cmd_conclude(root: Path, args: argparse.Namespace) -> str:
    if len(args.notes) > MAX_TEXT:
        raise ToolError(f"--notes exceeds {MAX_TEXT} chars")
    staged = _load(root)
    staged["notes"] = args.notes
    d = root / DIR
    d.mkdir(exist_ok=True)
    (d / ABI).write_text(json.dumps(staged))
    (d / STAGING).unlink(missing_ok=True)
    n = len(staged["findings"])
    blocking = sum(1 for f in staged["findings"] if f.get("blocking"))
    return (
        f"verdict recorded: {n} finding(s), {blocking} blocking. This is your "
        "final answer — end your turn."
    )


def cmd_status(root: Path, _args: argparse.Namespace) -> str:
    staged = _load(root)
    lines = [f"{len(staged['findings'])} finding(s) staged:"]
    for f in staged["findings"]:
        tag = "BLOCKING" if f.get("blocking") else f.get("kind", "note")
        lines.append(f"  - [{tag}] {f['file']}:{f.get('line') or '?'} — {f['summary']}")
    if staged.get("notes"):
        lines.append(f"  notes: {staged['notes']}")
    return "\n".join(lines)


def cmd_cancel(root: Path, _args: argparse.Namespace) -> str:
    (root / DIR / STAGING).unlink(missing_ok=True)
    return "staged findings discarded."


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="verdict", description="judge verdict tool")
    sub = p.add_subparsers(dest="cmd", required=True)
    fi = sub.add_parser("finding", help="record one finding")
    fi.add_argument("--file", required=True)
    fi.add_argument(
        "--line", type=int, default=None, help="1-indexed; omit for a non-local finding"
    )
    fi.add_argument("--confidence", required=True, help=f"one of {CONFIDENCES}")
    fi.add_argument("--summary", required=True, help="one-line claim")
    fi.add_argument("--detail", required=True, help="the evidence")
    fi.add_argument("--blocking", action="store_true", help="a confirmed defect that gates merge")
    fi.add_argument("--kind", default="note", help=f"one of {KINDS}")
    fi.add_argument("--category", default="", help="verifier gaming taxonomy; omit for review")
    co = sub.add_parser("conclude", help="commit the verdict; then end your turn")
    co.add_argument("--notes", default="", help="summary the reader sees")
    sub.add_parser("status", help="show staged findings")
    sub.add_parser("cancel", help="discard staged findings")
    return p


_HANDLERS = {
    "finding": cmd_finding,
    "conclude": cmd_conclude,
    "status": cmd_status,
    "cancel": cmd_cancel,
}


def main(argv: list[str], root: Path | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = root or Path.cwd()
    try:
        print(_HANDLERS[args.cmd](root, args))
        return 0
    except ToolError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover - exercised via main(argv) in tests
    with contextlib.suppress(BrokenPipeError):
        sys.exit(main(sys.argv[1:]))
