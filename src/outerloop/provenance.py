"""Snapshot the producing author before asynchronous work crosses a leg boundary."""

from pathlib import Path

from outerloop.runstate import RunRecord, load_record


def candidate_authors(record: RunRecord) -> dict[str, object]:
    saved = record.stage.get("candidate_authors")
    authors = dict(saved) if isinstance(saved, dict) else {}
    if sha := record.stage.get("candidate_sha"):
        authors.setdefault(
            str(sha),
            record.stage.get(
                "candidate_author",
                {
                    "backend": record.author_backend or "claude",
                    "model": record.author_model,
                },
            ),
        )
    return authors


def producing_author(directory: Path, commit: str = "") -> dict[str, object]:
    try:
        record = load_record(directory.parent.parent, directory.name)
    except FileNotFoundError:
        return {}  # Standalone evaluations and legacy data have no known author.
    previous = candidate_authors(record).get(commit)
    if isinstance(previous, dict):
        return dict(previous)
    return {"backend": record.author_backend or "claude", "model": record.author_model}
