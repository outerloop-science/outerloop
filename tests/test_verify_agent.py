"""Agent-verifier orchestration (run_agent_verify) with a fake harness and a
fake GitHub client — no real session, no network."""

from __future__ import annotations

import json
from pathlib import Path

from outerloop.harness import SessionResult
from outerloop.verifier import build_verify_agent_brief
from outerloop.verify_agent import run_agent_verify

_WORKSPACE = Path("/tmp/two-trees")
BOT = "agentic-learning-bot"

_FINDINGS = json.dumps(
    {
        "findings": [
            {
                "file": "models/encoder.py",
                "line": 12,
                "category": "ruler-fishing",
                "confidence": "medium",
                "summary": "constant matched to the frozen seed pool",
                "detail": "g=4 tracks the three fixed seeds.",
                "blocking": True,
            }
        ],
        "notes": "checked the ruler in base/",
    }
)


class _Harness:
    def __init__(self, final_text: str, *, is_error: bool = False, detail: str = "") -> None:
        self._text, self._err, self._detail = final_text, is_error, detail
        self.briefs: list[str] = []

    def run(
        self, brief_text: str, workspace: Path, resume_session_id: str | None = None
    ) -> SessionResult:
        self.briefs.append(brief_text)
        if not self._err:
            # commit the verdict through the syscall channel, as the tool would
            try:
                payload = json.loads(self._text)
            except (json.JSONDecodeError, TypeError):
                payload = None  # never concluded: no verdict written
            if isinstance(payload, dict):
                for f in payload.get("findings", []):
                    if isinstance(f, dict):
                        f.setdefault("kind", "note")  # the tool's own default
                d = Path(workspace) / ".outerloop"
                d.mkdir(exist_ok=True)
                (d / "syscall.json").write_text(json.dumps({"type": "verdict", **payload}))
        return SessionResult(
            stop_reason="error" if self._err else "completed",
            is_error=self._err,
            cost_usd=0.0,
            num_turns=1,
            session_id="s",
            final_text=self._text,
            transcript_path="",
            error_detail=self._detail,
        )


class _Client:
    """Minimal GitHubClient stand-in recording posts."""

    def __init__(self, *, author: str = BOT) -> None:
        self._author = author
        self.comments: list[str] = []

    def get_pull_request(self, repo: str, number: int) -> dict:
        return {
            "title": "[agent] heldout_probe: 0.46 -> 0.55",
            "body": "orchestrator-measured numbers plus my report",
            "user": {"login": self._author},
            "labels": [],
            "head": {"sha": "feedbeef1234"},
            "base": {"ref": "main"},
        }

    def get_pull_request_diff(self, repo: str, number: int) -> str:
        return "diff --git a/models/encoder.py b/models/encoder.py\n@@ -1 +1 @@\n+g = 4\n"

    def get_file_content(self, repo: str, path: str, ref: str) -> str:
        assert ref == "main", "contract must come from the BASE branch"
        return "benchmarks:\n  - name: heldout_probe\n"

    def list_comments(self, repo: str, number: int) -> list[dict]:
        return []

    def list_pr_reviews(self, repo: str, number: int) -> list[dict]:
        return []

    def list_pr_review_comments(self, repo: str, number: int) -> list[dict]:
        return []

    def comment(self, repo: str, number: int, body: str) -> None:
        self.comments.append(body)


def _run(client: _Client, harness: _Harness) -> str | None:
    return run_agent_verify(
        client,  # type: ignore[arg-type]
        "org/repo",
        9,
        harness,
        _WORKSPACE,
        bot_login=BOT,
    )


def test_bot_pr_is_verified_and_posts_issue_comment() -> None:
    client, harness = _Client(), _Harness(_FINDINGS)
    label = _run(client, harness)
    assert label is not None
    assert len(client.comments) == 1
    body = client.comments[0]
    assert "outerloop:verification-review" in body
    assert "ruler-fishing" in body or "constant matched" in body
    # the brief carried the two-tree instruction and the base-branch contract
    assert "pr-head/" in harness.briefs[0] and "base/" in harness.briefs[0]
    assert "heldout_probe" in harness.briefs[0]


def test_human_pr_is_skipped() -> None:
    # the verifier's skip is the INVERSE of the reviewer's: bot PRs only
    client, harness = _Client(author="alice"), _Harness(_FINDINGS)
    assert _run(client, harness) is None
    assert client.comments == []
    assert harness.briefs == []  # skipped before the session ran


def test_outage_posts_skip_stub() -> None:
    client = _Client()
    harness = _Harness("", is_error=True, detail="rate_limit_error: slow down")
    assert _run(client, harness) is None
    assert len(client.comments) == 1
    assert "could not run" in client.comments[0]


def test_malformed_output_posts_nothing() -> None:
    client, harness = _Client(), _Harness("not json at all")
    assert _run(client, harness) is None
    assert client.comments == []


def test_category_survives_the_result_policy() -> None:
    from outerloop.role_runner import RoleResult
    from outerloop.roles import verify_result_from_role

    role_result = RoleResult(
        ok=True,
        session=SessionResult("completed", False, 0.0, 1, "s", _FINDINGS, ""),
        data=json.loads(_FINDINGS),
    )
    result = verify_result_from_role(role_result)
    assert result is not None
    assert result.findings[0].category == "ruler-fishing"
    assert result.findings[0].blocking is True


def test_brief_directs_ruler_reads_at_base() -> None:
    from outerloop.review import PullRequest

    pr = PullRequest("org/repo", 9, "t", "b", "diff", BOT)
    brief = build_verify_agent_brief(pr, "contract: yes", today="2026-08-13")
    assert "Read the ruler source" in brief
    assert "`base/`" in brief and "`pr-head/`" in brief
    assert "contract: yes" in brief  # fenced from base, orchestrator-vouched
    assert "python .outerloop/syscall finding" in brief  # the emit path
    assert "do not modify" in brief.lower()


def test_bogus_category_clamps_to_other() -> None:
    from outerloop.verifier import verify_result_from_data

    data = {
        "findings": [
            {
                "file": "x.py",
                "line": 1,
                "category": "made-up-category",
                "confidence": "low",
                "summary": "s",
                "detail": "d",
                "blocking": False,
            }
        ],
        "notes": "",
    }
    assert verify_result_from_data(data).findings[0].category == "other"


def test_tokenless_split_emits_then_posts(tmp_path: Path) -> None:
    # The write-token split: run_agent_verify(emit_path=...) writes the verdict
    # to a file and posts NOTHING (the session job holds no write token); a
    # separate post step (verify_post_cli) reads it and posts.
    import json as _json

    from outerloop.verify_post_cli import post_from_file

    ws = tmp_path / "two-trees"
    ws.mkdir()
    emit = tmp_path / "verdict.json"
    session_client = _Client()
    label = run_agent_verify(
        session_client,  # type: ignore[arg-type]
        "org/repo",
        9,
        _Harness(_FINDINGS),
        ws,
        bot_login=BOT,
        emit_path=emit,
    )
    assert label == "emitted"
    assert session_client.comments == []  # the session job posts nothing
    envelope = _json.loads(emit.read_text())
    assert envelope["repo"] == "org/repo" and envelope["number"] == 9
    assert envelope["kind"] == "findings"

    post_client = _Client()  # the write-token job, no session
    round_label = post_from_file(post_client, "org/repo", 9, BOT, emit)  # type: ignore[arg-type]
    assert round_label is not None
    assert len(post_client.comments) == 1
    assert "outerloop:verification-review" in post_client.comments[0]


def test_post_from_file_refuses_a_mismatched_pr(tmp_path: Path) -> None:
    import json as _json

    from outerloop.verify_post_cli import post_from_file

    emit = tmp_path / "verdict.json"
    emit.write_text(_json.dumps({"repo": "org/repo", "number": 9, "kind": "findings", "data": {}}))
    client = _Client()
    # the artifact crosses a job boundary: an envelope naming another PR is refused
    assert post_from_file(client, "org/repo", 10, BOT, emit) is None  # type: ignore[arg-type]
    assert client.comments == []
