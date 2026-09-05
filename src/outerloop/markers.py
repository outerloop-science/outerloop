"""Body markers and labels the kernel writes and later recognizes.

The kernel finds its own past comments, issues, and claims by an HTML-comment
marker (`<!-- outerloop:advisory-review -->`), and routes work by labels
(`outerloop:review`). Both are WRITTEN under the new `outerloop:` prefix and
RECOGNIZED under both prefixes — a reviewer that failed to see its own earlier
`autoresearch:` comment would post a duplicate, and a target's existing
`autoresearch:steward` issues must keep routing. Reads go through `has_marker` /
`has_label`; writes and documentation use `marker` / `label_name`.
"""

from __future__ import annotations

from collections.abc import Iterable

NEW = "outerloop"
LEGACY = "autoresearch"
PREFIXES: tuple[str, ...] = (NEW, LEGACY)


def marker(kind: str) -> str:
    """The marker we write for `kind`, e.g. `<!-- outerloop:followup -->`."""
    return f"<!-- {NEW}:{kind} -->"


def legacy_marker(kind: str) -> str:
    """The pre-rename marker for `kind`; only for finding old text of ours."""
    return f"<!-- {LEGACY}:{kind} -->"


def has_marker(body: str, kind: str) -> bool:
    """Does `body` carry the `kind` marker under either prefix?"""
    return any(f"<!-- {prefix}:{kind} -->" in body for prefix in PREFIXES)


def label_name(kind: str) -> str:
    """The label we apply and document for `kind`, e.g. `outerloop:review`."""
    return f"{NEW}:{kind}"


def is_label(name: str, kind: str) -> bool:
    """Is `name` the `kind` label under either prefix? Case-insensitive, as
    GitHub label matching is."""
    return name.casefold() in {f"{prefix}:{kind}" for prefix in PREFIXES}


def has_label(labels: Iterable[str], kind: str) -> bool:
    return any(is_label(name, kind) for name in labels)
