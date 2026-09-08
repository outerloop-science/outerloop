"""In-process test doubles for the kernel's seams."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from outerloop.harness import SessionResult
from outerloop.runstate import RunRecord


@dataclass
class FakeHarness:
    """Deterministic in-process harness: records calls, returns a canned result."""

    result: SessionResult
    script: Any = None  # optional callable(brief_text, workspace) for side effects
    calls: list[tuple[str, str, str | None]] = field(default_factory=list)
    supports_resume: bool = True  # a field so tests can exercise the no-resume path

    def run(
        self, brief_text: str, workspace: Path, resume_session_id: str | None = None
    ) -> SessionResult:
        self.calls.append((brief_text, str(workspace), resume_session_id))
        if self.script is not None:
            self.script(brief_text, workspace)
        return self.result


@dataclass
class RecordingDispatcher:
    """Dispatcher that records what would have been woken."""

    dispatched: list[tuple[str, str]] = field(default_factory=list)
    holder_job_id: str = ""  # set to simulate async dispatch

    def dispatch(self, record: RunRecord, reason: str) -> str:
        self.dispatched.append((record.run_id, reason))
        return self.holder_job_id
