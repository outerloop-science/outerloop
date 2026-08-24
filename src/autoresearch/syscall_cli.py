#!/usr/bin/env python3
"""The author's launch/sleep tool — the agent-facing surface for author
syscalls (research-loop.md, "one syscall, author-directed").

This file is STANDALONE by contract: the kernel copies its source into the
sandbox at `.autoresearch/syscall` (the target repo does not have autoresearch
installed), so it imports only the stdlib. The author calls it like a CLI and
never hand-writes JSON:

    python .autoresearch/syscall launch --name train --minutes 90 \\
        --artifact results/curve.json -- uv run python train.py --lr 3e-4
    python .autoresearch/syscall note "compare with the lr sweep"
    python .autoresearch/syscall status
    python .autoresearch/syscall sleep       # then END YOUR TURN to hibernate

`launch`/`note` stage into `.autoresearch/request.json`; `sleep` commits the
staged request to `.autoresearch/syscall.json` (the kernel ABI it reads after
the session ends) — so building a request and committing to hibernate are
separate acts, and an in-progress request never triggers a sleep. `sleep` with
nothing staged is a checkpoint sleep (re-schedule me, fresh clock).

The validation here is for FAST, IN-SESSION feedback only. The kernel
re-validates every field authoritatively when it reads the ABI (syscall.py) —
this tool is a convenience layer, never a trust boundary, so an author that
writes the ABI directly is still fully checked.
"""

from __future__ import annotations

import argparse
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
ABI = "syscall.json"  # committed request the kernel reads
BUDGET = "budget.json"  # kernel-written: remaining counts, for `status`
MAX_LAUNCHES = 8
MAX_COMMAND_CHARS = 2_000
MAX_ARTIFACTS = 8
MAX_NOTE_CHARS = 2_000
MAX_LAUNCH_MINUTES = 240
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
        return {"launches": [], "note": ""}
    except (OSError, json.JSONDecodeError) as exc:
        raise ToolError(f"staged request is unreadable ({exc}); run `cancel` to reset") from exc
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


def cmd_launch(root: Path, args: argparse.Namespace) -> str:
    # shlex.join, NOT " ".join: the shell that invoked this CLI already split
    # `-- python train.py --label "a b"` into tokens, so re-quote them so the
    # eventual `sh -c "$(cat command.txt)"` re-parses the SAME tokens (a plain
    # join would collapse `a b` into two args — terra #133 r1).
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


def cmd_status(root: Path, _args: argparse.Namespace) -> str:
    staged = _load_staged(root)
    lines = [f"{len(staged['launches'])} launch(es) staged; {_budget_line(root)}."]
    for la in staged["launches"]:
        arts = (" -> " + ", ".join(la["artifacts"])) if la.get("artifacts") else ""
        lines.append(f"  - {la['name']} ({la['minutes']} min): {la['command']}{arts}")
    if staged.get("note"):
        lines.append(f"  note: {staged['note']}")
    return "\n".join(lines)


def cmd_cancel(root: Path, _args: argparse.Namespace) -> str:
    (root / DIR / REQUEST).unlink(missing_ok=True)
    return "staged request discarded."


def cmd_sleep(root: Path, _args: argparse.Namespace) -> str:
    staged = _load_staged(root)
    # commit staging -> the ABI file the kernel reads; then END THE TURN.
    (_dir(root) / ABI).write_text(json.dumps(staged))
    (root / DIR / REQUEST).unlink(missing_ok=True)
    n = len(staged["launches"])
    what = f"{n} launch(es)" if n else "a checkpoint (no launches)"
    return (
        f"committed {what}. END YOUR TURN NOW to hibernate — you will be woken "
        "with the results. (If you keep working, the sleep still triggers when "
        "the session ends.)"
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="syscall", description="author launch/sleep tool")
    sub = p.add_subparsers(dest="cmd", required=True)
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
    sub.add_parser("status", help="show staged launches and remaining budget")
    sub.add_parser("cancel", help="discard the staged request")
    sub.add_parser("sleep", help="commit the staged request; then end your turn")
    return p


_HANDLERS = {
    "launch": cmd_launch,
    "note": cmd_note,
    "status": cmd_status,
    "cancel": cmd_cancel,
    "sleep": cmd_sleep,
}


def main(argv: list[str], root: Path | None = None) -> int:
    args = build_parser().parse_args(argv)
    # argparse REMAINDER keeps a leading "--"; drop it for a clean command
    if getattr(args, "command", None) and args.command and args.command[0] == "--":
        args.command = args.command[1:]
    root = root or Path.cwd()
    try:
        print(_HANDLERS[args.cmd](root, args))
        return 0
    except ToolError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover - exercised via main(argv) in tests
    sys.exit(main(sys.argv[1:]))
