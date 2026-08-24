"""Judge verdicts: the kernel side of the verdict tool (docs/design/role-cli.md
Phase 2).

The judge's interface is `verdict_cli.py` (installed at `.verdict/verdict`); it
commits a verdict to `.verdict/verdict.json` on `conclude`. This module is the
KERNEL side — `read_verdict` is the authoritative validator (the tool is
agent-controlled once dropped, so it is never trusted), returning the SAME
`{findings, notes}` shape `run_role` used to parse from the final message, minus
the parse-and-repair loop. `install_tool` drops the standalone tool into the
judge's workspace.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

VERDICT_DIR = ".verdict"
VERDICT_FILE = "verdict.json"
MAX_VERDICT_BYTES = 1_000_000  # generous; agent-controlled, so size-capped first
CONFIDENCES = frozenset({"low", "medium", "high"})
KINDS = frozenset({"change", "suggestion", "question", "note"})


class VerdictError(ValueError):
    """The committed verdict is missing or malformed. Loud: a judge that ran
    the tool meant a verdict, so a broken file is an error, never a silent
    empty pass (silence is never endorsement)."""


def read_verdict(workspace: Path) -> dict[str, Any] | None:
    """Read and validate the committed verdict. None = the judge never
    concluded (no file) — the caller treats that as no-verdict, exactly like an
    errored session. A present-but-malformed verdict raises VerdictError.

    Validates every field the schema requires (the tool's checks are advisory);
    an unknown enum, a wrong type, or a missing key fails here — the verdict is
    well-formed after this returns."""
    path = workspace / VERDICT_DIR / VERDICT_FILE
    try:
        with path.open("rb") as fh:
            head = fh.read(MAX_VERDICT_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise VerdictError(f"verdict unreadable: {exc}") from exc
    if len(head) > MAX_VERDICT_BYTES:
        raise VerdictError(f"verdict exceeds {MAX_VERDICT_BYTES} bytes")
    try:
        data = json.loads(head.decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        raise VerdictError(f"verdict is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise VerdictError("verdict must be a JSON object")
    if "notes" not in data:
        raise VerdictError("verdict is missing required key: notes")
    notes = data["notes"]
    if not isinstance(notes, str):
        raise VerdictError("notes must be a string")
    raw = data.get("findings")
    if not isinstance(raw, list):
        raise VerdictError("findings must be a list")
    findings = [_validate_finding(i, item) for i, item in enumerate(raw)]
    return {"findings": findings, "notes": notes}


_REQUIRED_FINDING_KEYS = ("file", "line", "confidence", "summary", "detail", "blocking", "kind")


def _validate_finding(i: int, item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise VerdictError(f"finding #{i} must be an object")
    file = item.get("file")
    if not isinstance(file, str) or not file:
        raise VerdictError(f"finding #{i}: file must be a non-empty string")
    # ENFORCE the schema's required keys — do not default them. Defaulting
    # `blocking` to False in particular is a fail-open: a finding that omits it
    # would silently not gate ("silence is never endorsement"). The tool always
    # emits every key, so this only rejects a malformed hand-written verdict
    # (the tool is not the trust boundary — terra #136 r3).
    missing = [k for k in _REQUIRED_FINDING_KEYS if k not in item]
    if missing:
        raise VerdictError(f"finding {file}: missing required keys {missing}")
    line = item["line"]
    if line is not None and (not isinstance(line, int) or isinstance(line, bool) or line < 1):
        raise VerdictError(f"finding {file}: line must be a positive (1-indexed) integer or null")
    confidence = item["confidence"]
    if confidence not in CONFIDENCES:
        raise VerdictError(f"finding {file}: confidence must be one of {sorted(CONFIDENCES)}")
    kind = item["kind"]
    if kind not in KINDS:
        raise VerdictError(f"finding {file}: kind must be one of {sorted(KINDS)}")
    for key in ("summary", "detail"):
        if not isinstance(item[key], str) or not item[key]:
            raise VerdictError(f"finding {file}: {key} must be a non-empty string")
    blocking = item["blocking"]
    if not isinstance(blocking, bool):
        raise VerdictError(f"finding {file}: blocking must be a boolean")
    out = {
        "file": file,
        "line": line,
        "confidence": confidence,
        "summary": item["summary"],
        "detail": item["detail"],
        "blocking": blocking,
        "kind": kind,
    }
    category = item.get("category", "")
    if category:  # verifier-only; keep only when the judge set it
        if not isinstance(category, str):
            raise VerdictError(f"finding {file}: category must be a string")
        # CLAMP an unknown category to "other" rather than reject — same stance
        # as the existing verifier path (verifier.py: "a free-string category
        # must not leak through"), so a taxonomy typo normalizes instead of
        # nuking a whole verdict. (terra #136 r1)
        from autoresearch.verifier import CATEGORIES

        out["category"] = category if category in CATEGORIES else "other"
    return out


def install_tool(workspace: Path) -> None:
    """Drop the standalone verdict tool into the judge's workspace at
    `.verdict/verdict`. Verbatim copy of `verdict_cli.py` (stdlib-only, since a
    judge runs in a prepared checkout without autoresearch installed).

    The `.verdict` channel must be KERNEL-OWNED: the judge's checkout is the
    author's tree, which could ship `.verdict` as a symlink to a host path so
    `write_text` writes through it (terra #136 r2, same class as the syscall
    channel). Remove any pre-existing `.verdict` (symlink → unlink, dir →
    rmtree, file → unlink) and recreate it as a dir we own, so nothing is
    followed."""
    import shutil

    from autoresearch import verdict_cli

    channel = workspace / VERDICT_DIR
    if channel.is_symlink() or (channel.exists() and not channel.is_dir()):
        channel.unlink()
    elif channel.is_dir():
        shutil.rmtree(channel)
    channel.mkdir(parents=True)
    tool = channel / "verdict"
    tool.write_text(Path(verdict_cli.__file__).read_text())
    tool.chmod(0o755)
