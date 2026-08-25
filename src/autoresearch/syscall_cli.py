#!/usr/bin/env python3
"""The research syscall tool — the one agent-facing surface every role uses to
talk to the kernel (research-loop.md, "one syscall"; role-cli.md, "one CLI per
role, gated by RoleSpec").

A syscall is TYPED, and the kernel dispatches by type. The AUTHOR's syscalls run
experiments and hibernate:

    python .autoresearch/syscall launch --name train --minutes 90 \\
        --artifact results/curve.json -- uv run python train.py --lr 3e-4
    python .autoresearch/syscall note "compare with the lr sweep"
    python .autoresearch/syscall submit      # seal + gate + panel on this tree
    python .autoresearch/syscall sleep       # then END YOUR TURN to hibernate

The JUDGE's syscalls record a verdict and exit — `conclude` is the judge's
`exit()`, carrying its findings:

    python .autoresearch/syscall finding --file solver.py --line 42 \\
        --confidence high --summary "off-by-one" --detail "skips last index" --blocking
    python .autoresearch/syscall conclude --notes "one blocking defect; rest clean"

Which verbs a role may use is set by its RoleSpec (the brief tells the role
which). Every verb STAGES into `.autoresearch/request.json`; the committing
verbs (`sleep`, `conclude`) write the typed ABI to `.autoresearch/syscall.json`
(what the kernel reads after the session ends) — so building a request and
committing it are separate acts.

This file is STANDALONE by contract: the kernel copies its source into the
sandbox at `.autoresearch/syscall` (the target repo does not have autoresearch
installed), so it imports only the stdlib. The validation here is for FAST,
IN-SESSION feedback only; the kernel re-validates every field authoritatively
when it reads the ABI (`syscall.py`) — this tool is a convenience layer, never a
trust boundary, so a role that writes the ABI directly is still fully checked.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import shlex
import sys
from pathlib import Path

# Mirror of syscall.py's bounds for local feedback. syscall.py is authoritative;
# keep these in sync (a drift only makes the tool's warning stale, never unsafe —
# the kernel still enforces the real limits).
DIR = ".autoresearch"
REQUEST = "request.json"  # staging (tool-owned)
ABI = "syscall.json"  # committed syscall the kernel reads
BUDGET = "budget.json"  # kernel-written: remaining counts, for `status`
# author (launch/sleep) bounds
MAX_LAUNCHES = 8
MAX_COMMAND_CHARS = 2_000
MAX_ARTIFACTS = 8
MAX_NOTE_CHARS = 2_000
MAX_LAUNCH_MINUTES = 240
# judge (finding/conclude) bounds
CONFIDENCES = ("low", "medium", "high")
KINDS = ("change", "suggestion", "question", "note")
MAX_TEXT = 6_000  # per summary/detail/notes/category
MAX_FINDINGS = 200
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")


class ToolError(Exception):
    """A bad invocation: printed to stderr, exit 2, nothing staged."""


def _rel_path_ok(path: str) -> bool:
    """MUST match syscall._rel_path_ok exactly — the CLI's fast check has to
    accept precisely what the kernel accepts, or the author burns a sleep on a
    post-session validation error (the very thing the tool exists to prevent).
    Rejects absolute/`~`, backslashes, over-long, and any empty/`.`/`..`
    component (so ``, `.`, `out/./x`, `out//x` all fail here as they do there)."""
    if not path or len(path) > 500 or path.startswith(("/", "~")) or "\\" in path:
        return False
    return all(p not in ("", ".", "..") for p in path.split("/"))


def _dir(root: Path) -> Path:
    d = root / DIR
    d.mkdir(exist_ok=True)
    return d


def _load_staged(root: Path) -> dict:
    f = root / DIR / REQUEST
    try:
        data = json.loads(f.read_text())
    except FileNotFoundError:
        return {"launches": [], "note": "", "submit": False, "findings": [], "notes": ""}
    except (OSError, json.JSONDecodeError) as exc:
        raise ToolError(f"staged request is unreadable ({exc}); run `cancel` to reset") from exc
    # tolerate a partial file: default any missing family so either role's verbs work
    for key, empty in (
        ("launches", []),
        ("note", ""),
        ("submit", False),
        ("findings", []),
        ("notes", ""),
    ):
        data.setdefault(key, empty)
    return data


def _save_staged(root: Path, data: dict) -> None:
    (_dir(root) / REQUEST).write_text(json.dumps(data, indent=2))


def _budget_line(root: Path) -> str:
    try:
        b = json.loads((root / DIR / BUDGET).read_text())
        return (
            f"budget: {b.get('launches_remaining', '?')} launches, "
            f"{b.get('sleeps_remaining', '?')} sleeps remaining"
        )
    except (OSError, json.JSONDecodeError):
        return "budget: (unknown)"


# --- author syscalls: launch / note / sleep --------------------------------


def cmd_launch(root: Path, args: argparse.Namespace) -> str:
    # shlex.join, NOT " ".join: the shell that invoked this CLI already split
    # `-- python train.py --label "a b"` into tokens, so re-quote them so the
    # eventual `sh -c "$(cat command.txt)"` re-parses the SAME tokens (a plain
    # join would collapse `a b` into two args).
    command = shlex.join(args.command).strip()
    if not command:
        raise ToolError("launch needs a command after `--`")
    if len(command) > MAX_COMMAND_CHARS:
        raise ToolError(f"command exceeds {MAX_COMMAND_CHARS} chars")
    if not _NAME.match(args.name):
        raise ToolError(f"--name must match {_NAME.pattern}")
    if args.minutes < 1:
        raise ToolError("--minutes must be a positive integer")
    minutes = min(args.minutes, MAX_LAUNCH_MINUTES)
    if len(args.artifact) > MAX_ARTIFACTS:
        raise ToolError(f"at most {MAX_ARTIFACTS} --artifact paths")
    for a in args.artifact:
        if not _rel_path_ok(a):
            raise ToolError(f"--artifact {a!r} must be a repo-relative file path, no traversal")
    staged = _load_staged(root)
    if any(la["name"] == args.name for la in staged["launches"]):
        raise ToolError(f"a launch named {args.name!r} is already staged")
    if len(staged["launches"]) >= MAX_LAUNCHES:
        raise ToolError(f"at most {MAX_LAUNCHES} launches per sleep")
    staged["launches"].append(
        {"name": args.name, "command": command, "minutes": minutes, "artifacts": args.artifact}
    )
    _save_staged(root, staged)
    return (
        f"staged launch {args.name!r} ({minutes} min); {len(staged['launches'])} staged. "
        f"Add more, or `sleep` to run them. {_budget_line(root)}."
    )


def cmd_note(root: Path, args: argparse.Namespace) -> str:
    note = args.text
    if len(note) > MAX_NOTE_CHARS:
        raise ToolError(f"note exceeds {MAX_NOTE_CHARS} chars")
    staged = _load_staged(root)
    staged["note"] = note
    _save_staged(root, staged)
    return "note saved (delivered back to you on wake)."


def cmd_submit(root: Path, _args: argparse.Namespace) -> str:
    staged = _load_staged(root)
    staged["submit"] = True
    _save_staged(root, staged)
    return (
        "staged submit: on `sleep` your current tree is SEALED and measured "
        "against the baseline, and the review panel reads the claim; you will "
        "be woken with the result (published if it clears cleanly). "
        f"{_budget_line(root)}."
    )


def cmd_sleep(root: Path, _args: argparse.Namespace) -> str:
    staged = _load_staged(root)
    # commit the SLEEP syscall -> the ABI the kernel reads; then END THE TURN.
    payload = {
        "type": "sleep",
        "launches": staged["launches"],
        "note": staged["note"],
        "submit": bool(staged["submit"]),
    }
    (_dir(root) / ABI).write_text(json.dumps(payload))
    (root / DIR / REQUEST).unlink(missing_ok=True)
    n = len(staged["launches"])
    what = f"{n} launch(es)" if n else "a checkpoint (no launches)"
    if staged["submit"]:
        what += " + a submit (seal, gate, panel)"
    return (
        f"committed {what}. END YOUR TURN NOW to hibernate — you will be woken "
        "with the results. (If you keep working, the sleep still triggers when "
        "the session ends.)"
    )


# --- judge syscalls: finding / conclude ------------------------------------


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
    staged = _load_staged(root)
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
    _save_staged(root, staged)
    tag = "BLOCKING" if args.blocking else args.kind
    where = f"{args.file}:{args.line or '?'}"
    return f"recorded {tag} finding on {where} ({len(staged['findings'])} so far)."


def cmd_conclude(root: Path, args: argparse.Namespace) -> str:
    if len(args.notes) > MAX_TEXT:
        raise ToolError(f"--notes exceeds {MAX_TEXT} chars")
    staged = _load_staged(root)
    # commit the VERDICT syscall -> the ABI the kernel reads; then END THE TURN.
    payload = {"type": "verdict", "findings": staged["findings"], "notes": args.notes}
    (_dir(root) / ABI).write_text(json.dumps(payload))
    (root / DIR / REQUEST).unlink(missing_ok=True)
    n = len(staged["findings"])
    blocking = sum(1 for f in staged["findings"] if f.get("blocking"))
    return (
        f"verdict recorded: {n} finding(s), {blocking} blocking. This is your "
        "final answer — end your turn."
    )


# --- shared: status / cancel -----------------------------------------------


def cmd_status(root: Path, _args: argparse.Namespace) -> str:
    staged = _load_staged(root)
    lines: list[str] = []
    if staged["launches"] or staged["submit"] or (root / DIR / BUDGET).exists():
        lines.append(f"{len(staged['launches'])} launch(es) staged; {_budget_line(root)}.")
        for la in staged["launches"]:
            arts = (" -> " + ", ".join(la["artifacts"])) if la.get("artifacts") else ""
            lines.append(f"  - {la['name']} ({la['minutes']} min): {la['command']}{arts}")
        if staged["submit"]:
            lines.append("  submit staged: `sleep` seals this tree for the gate + panel")
        if staged.get("note"):
            lines.append(f"  note: {staged['note']}")
    if staged["findings"]:
        lines.append(f"{len(staged['findings'])} finding(s) staged:")
        for f in staged["findings"]:
            tag = "BLOCKING" if f.get("blocking") else f.get("kind", "note")
            lines.append(f"  - [{tag}] {f['file']}:{f.get('line') or '?'} — {f['summary']}")
    return "\n".join(lines) if lines else "nothing staged."


def cmd_cancel(root: Path, _args: argparse.Namespace) -> str:
    (root / DIR / REQUEST).unlink(missing_ok=True)
    return "staged request discarded."


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="syscall", description="research syscall tool")
    sub = p.add_subparsers(dest="cmd", required=True)
    # author verbs
    la = sub.add_parser("launch", help="stage a job to run outside the sandbox")
    la.add_argument("--name", required=True, help="your handle for this job (a-z0-9-)")
    la.add_argument("--minutes", type=int, default=30, help="walltime ask (clamped to 240)")
    la.add_argument(
        "--artifact",
        action="append",
        default=[],
        help="repo-relative file to bring back (repeatable)",
    )
    la.add_argument("command", nargs=argparse.REMAINDER, help="-- then the command to run")
    no = sub.add_parser("note", help="save a note to yourself, echoed back on wake")
    no.add_argument("text")
    sub.add_parser(
        "submit",
        help="stage a submit: on sleep, seal this tree for the gate + review panel",
    )
    sub.add_parser("sleep", help="commit staged launches/submit; then end your turn")
    # judge verbs
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
    # shared verbs
    sub.add_parser("status", help="show staged syscalls and remaining budget")
    sub.add_parser("cancel", help="discard the staged request")
    return p


_HANDLERS = {
    "launch": cmd_launch,
    "note": cmd_note,
    "submit": cmd_submit,
    "sleep": cmd_sleep,
    "finding": cmd_finding,
    "conclude": cmd_conclude,
    "status": cmd_status,
    "cancel": cmd_cancel,
}


def main(argv: list[str], root: Path | None = None) -> int:
    args = build_parser().parse_args(argv)
    # argparse REMAINDER keeps a leading "--"; drop it for a clean command
    if getattr(args, "command", None) and args.command and args.command[0] == "--":
        args.command = args.command[1:]
    # Root at the tool's own install location (<workspace>/.autoresearch/
    # syscall -> the workspace), NEVER the caller's cwd: an agent may invoke
    # the tool from a subdirectory or from another working directory entirely
    # (hermes starts in its per-run home), and a cwd-rooted channel would
    # silently commit the syscall where the kernel never looks.
    root = root or Path(__file__).resolve().parent.parent
    try:
        print(_HANDLERS[args.cmd](root, args))
        return 0
    except ToolError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover - exercised via main(argv) in tests
    with contextlib.suppress(BrokenPipeError):
        sys.exit(main(sys.argv[1:]))
