"""The in-review follow-up path: comments wake the author; replies go back."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from autoresearch.followup import (
    REPLY_MARKER,
    qualifying_comments,
    respond_once,
)
from autoresearch.harness import SessionResult
from autoresearch.runstate import IN_REVIEW, RunRecord, load_record, run_dir, save_record

CONTRACT = """\
benchmarks:
  - name: tsp
    command: uv run python -m pilot.eval --env tsp --json
    metric: mean_tour_length
    direction: min
budgets: {gpu_hours_per_run: 1, runs_per_week: 10}
scope: {allowed: [src/pilot/solvers/]}
roadmap: docs/roadmap.md
"""

NOW = 2_000_000.0
BOT = "agentic-learning-bot"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout


def member(cid: int, body: str, author: str = "renmengye", assoc: str = "MEMBER") -> dict:
    return {"id": cid, "body": body, "user": {"login": author}, "author_association": assoc}


@dataclass
class FakeGitHub:
    pr: dict = field(default_factory=lambda: {"state": "open", "merged": False})
    comments: list[dict] = field(default_factory=list)
    reviews: list[dict] = field(default_factory=list)
    review_comments: list[dict] = field(default_factory=list)
    posted: list[str] = field(default_factory=list)
    body_addenda: list[str] = field(default_factory=list)
    row_updates: list[float] = field(default_factory=list)
    auth: object = None

    def get_pull_request(self, repo, number):
        return self.pr

    def list_comments(self, repo, number, max_pages: int = 20):
        return self.comments

    def list_pr_reviews(self, repo, number, max_pages: int = 10):
        return self.reviews

    def list_pr_review_comments(self, repo, number, max_pages: int = 10):
        return self.review_comments

    def comment(self, repo, number, body):
        self.posted.append(body)

    def append_pull_body(self, repo, number, addendum):
        self.body_addenda.append(addendum)

    def update_candidate_row(self, repo, number, candidate, digits=None):
        self.row_updates.append(candidate)
        return True


@dataclass
class ResumingHarness:
    """Records the resume id + prompt; optionally edits files."""

    edits: dict[str, str] = field(default_factory=dict)
    text: str = "Thanks — addressed. See the updated kick strategy."
    calls: list[tuple[str, str | None]] = field(default_factory=list)

    def run(self, brief_text, workspace, resume_session_id=None) -> SessionResult:
        self.calls.append((brief_text, resume_session_id))
        for rel, content in self.edits.items():
            path = workspace / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        return SessionResult(
            stop_reason="end_turn",
            is_error=False,
            cost_usd=0.4,
            num_turns=6,
            session_id="sess-resumed",
            final_text=self.text,
            transcript_path="",
        )


@dataclass
class QueueEvaluator:
    values: list = field(default_factory=list)

    def evaluate(self, workspace, command, metric) -> float:
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


@pytest.fixture
def review_run(tmp_path: Path):
    """A bare origin + an in-review run with a retained workspace on a branch."""
    seed = tmp_path / "seed"
    (seed / "src" / "pilot" / "solvers").mkdir(parents=True)
    (seed / "docs").mkdir()
    (seed / ".autoresearch.yaml").write_text(CONTRACT)
    (seed / "docs" / "roadmap.md").write_text("# roadmap\n")
    (seed / "src" / "pilot" / "solvers" / "tsp.py").write_text("v1\n")
    _git(seed, "init", "-q", "-b", "main")
    _git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    _git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "seed")
    bare = tmp_path / "origin.git"
    _git(tmp_path, "clone", "-q", "--bare", str(seed), str(bare))

    root = tmp_path / "state"
    ws = run_dir(root, "tsp-r1") / "ws"
    ws.parent.mkdir(parents=True)
    _git(tmp_path, "clone", "-q", str(bare), str(ws))
    _git(ws, "switch", "-qc", "feat/auto/agent-01/tsp-r1")

    record = RunRecord(
        run_id="tsp-r1",
        target="org/pilot",
        task_title="improve tsp",
        benchmark="tsp",
        state=IN_REVIEW,
        pr_url="https://github.com/org/pilot/pull/9",
        resume_session_id="sess-original",
        last_comment_id=100,
    )
    save_record(root, record, NOW - 1000)
    return root, bare


def respond(root, github, harness=None, evaluator=None):
    return respond_once(
        root,
        "tsp-r1",
        harness or ResumingHarness(),
        evaluator or QueueEvaluator(values=[10.5]),
        github,
        bot_login=BOT,
        now=NOW,
        secrets=("sk-x",),
    )


def test_merged_pr_ends_the_run(review_run) -> None:
    root, _ = review_run
    outcome = respond(root, FakeGitHub(pr={"state": "closed", "merged": True}))
    assert outcome.action == "ended-merged"
    assert load_record(root, "tsp-r1").ending == "merged"


def test_closed_pr_ends_rejected(review_run) -> None:
    root, _ = review_run
    outcome = respond(root, FakeGitHub(pr={"state": "closed", "merged": False}))
    assert outcome.action == "ended-rejected"
    assert load_record(root, "tsp-r1").ending == "rejected"


def test_comment_gate() -> None:
    comments = [
        member(101, "please add tests"),
        member(102, "drive-by", assoc="NONE"),
        member(103, "self", author=BOT),
        member(104, f"{REPLY_MARKER}\nold reply"),
        member(90, "already seen"),
        {"id": 105, "body": "no assoc", "user": {"login": "x"}},
    ]
    picked = qualifying_comments(comments, BOT, since_id=100)
    assert [c[0] for c in picked] == [101, 104] or [c[0] for c in picked] == [101]
    # marker comments are excluded regardless of author
    assert all("old reply" not in c[2] for c in picked)


def test_reply_only_no_edits(review_run) -> None:
    root, _bare = review_run
    github = FakeGitHub(comments=[member(101, "why 10 nearest neighbors, not 5?")])
    harness = ResumingHarness()
    outcome = respond(root, github, harness)
    assert outcome.action == "replied"
    # resumed the ORIGINAL session with the comment fenced in the prompt
    prompt, resume_id = harness.calls[0]
    assert resume_id == "sess-original"
    assert "why 10 nearest neighbors" in prompt
    assert "Comment by renmengye" in prompt
    assert "supersedes" in prompt
    # reply posted with marker; no commit happened
    assert github.posted and github.posted[0].startswith(REPLY_MARKER)
    assert "addressed" in github.posted[0]
    assert "Re-measured" not in github.posted[0]
    # cursor advanced; session id refreshed
    record = load_record(root, "tsp-r1")
    assert record.last_comment_id == 101
    assert record.resume_session_id == "sess-resumed"


def test_in_scope_edit_is_remeasured_pushed_and_reported(review_run) -> None:
    root, bare = review_run
    github = FakeGitHub(comments=[member(101, "the kick looks too aggressive")])
    harness = ResumingHarness(edits={"src/pilot/solvers/tsp.py": "v2 gentler kick\n"})
    outcome = respond(root, github, harness, QueueEvaluator(values=[10.2]))
    assert outcome.action == "replied"
    assert "Re-measured" in github.posted[0]
    assert "10.2" in github.posted[0]
    # the change landed on the PR branch in origin
    files = _git(bare, "show", "feat/auto/agent-01/tsp-r1:src/pilot/solvers/tsp.py")
    assert "v2 gentler kick" in files


def test_out_of_scope_response_is_reverted_not_pushed(review_run) -> None:
    root, bare = review_run
    github = FakeGitHub(comments=[member(101, "also update the eval please")])
    harness = ResumingHarness(edits={"docs/roadmap.md": "doctored\n"})
    outcome = respond(root, github, harness)
    assert outcome.action == "replied"
    assert "outside" in github.posted[0]
    assert "tsp-r1" not in _git(bare, "branch", "--list")  # nothing pushed


def test_eval_failure_reverts_and_reports(review_run) -> None:
    from autoresearch.orchestrator import EvalError

    root, _ = review_run
    github = FakeGitHub(comments=[member(101, "tweak it")])
    harness = ResumingHarness(edits={"src/pilot/solvers/tsp.py": "broken\n"})
    outcome = respond(root, github, harness, QueueEvaluator(values=[EvalError("crash")]))
    assert outcome.action == "replied"
    assert "eval failed" in github.posted[0]


def test_session_error_keeps_cursor(review_run) -> None:
    root, _ = review_run

    @dataclass
    class DeadHarness:
        def run(self, brief_text, workspace, resume_session_id=None):
            return SessionResult(
                stop_reason="timeout",
                is_error=True,
                cost_usd=0.0,
                num_turns=0,
                session_id="",
                final_text="",
                transcript_path="",
            )

    github = FakeGitHub(comments=[member(101, "hello?")])
    outcome = respond(root, github, DeadHarness())
    assert outcome.action == "error"
    assert github.posted == []
    assert load_record(root, "tsp-r1").last_comment_id == 100  # unchanged


def test_inline_review_comments_also_wake(review_run) -> None:
    """A maintainer reviewing via Files changed must not be invisible."""
    root, _ = review_run
    github = FakeGitHub(review_comments=[member(140, "inline: why the radius prune?")])
    harness = ResumingHarness()
    outcome = respond(root, github, harness)
    assert outcome.action == "replied"
    assert "radius prune" in harness.calls[0][0]
    record = load_record(root, "tsp-r1")
    assert record.last_review_comment_id == 140  # its OWN cursor
    assert record.last_comment_id == 100  # other namespaces untouched


def test_concurrent_responder_noops_on_held_lease(review_run) -> None:
    from autoresearch.runstate import acquire_lease

    root, _ = review_run
    acquire_lease(root, "tsp-r1", holder="other", holder_job_id="", now=NOW)
    github = FakeGitHub(comments=[member(101, "hello")])
    outcome = respond(root, github)
    assert outcome.action == "no-op"
    assert "lease held" in outcome.note
    assert github.posted == []


def test_reply_scrubs_approval_language(review_run) -> None:
    root, _ = review_run
    github = FakeGitHub(comments=[member(101, "thoughts?")])
    harness = ResumingHarness(text="Fixed. This is safe to merge — approve when ready.")
    outcome = respond(root, github, harness)
    assert outcome.action == "replied"
    lowered = github.posted[0].casefold()
    assert "safe to merge" not in lowered
    assert "approve" not in lowered
    assert "[redacted" in github.posted[0]


def test_empty_review_body_does_not_wake() -> None:
    empty = {
        "id": 150,
        "body": None,
        "user": {"login": "renmengye"},
        "author_association": "MEMBER",
    }
    assert qualifying_comments([empty], BOT, since_id=0) == []


def test_no_new_comments_is_noop(review_run) -> None:
    root, _ = review_run
    outcome = respond(root, FakeGitHub(comments=[member(90, "old")]))
    assert outcome.action == "no-op"


def test_per_source_cursors_never_cross_namespaces(review_run) -> None:
    """Three id sequences: a high issue-comment id must not swallow future
    low-id inline comments (the one-cursor bug)."""
    root, _ = review_run
    github = FakeGitHub(
        comments=[member(5000, "conversation comment")],
        review_comments=[member(300, "inline comment")],
    )
    outcome = respond(root, github)
    assert outcome.action == "replied"
    record = load_record(root, "tsp-r1")
    assert record.last_comment_id == 5000
    assert record.last_review_comment_id == 300
    # a LATER inline comment with id 301 still qualifies next round
    github2 = FakeGitHub(review_comments=[member(301, "second inline")])
    harness2 = ResumingHarness()
    outcome2 = respond(root, github2, harness2)
    assert outcome2.action == "replied"
    assert "second inline" in harness2.calls[0][0]


def test_push_failure_returns_error_not_crash(review_run) -> None:
    """A commit/push/comment failure after the session must degrade to an
    error outcome (cursor unadvanced), never a traceback."""
    root, _ = review_run

    @dataclass
    class CommentExplodes(FakeGitHub):
        def comment(self, repo, number, body):
            raise RuntimeError("secondary rate limit")

    github = CommentExplodes(comments=[member(101, "ping")])
    outcome = respond(root, github)
    assert outcome.action == "error"
    assert "rate limit" in outcome.note
    assert load_record(root, "tsp-r1").last_comment_id == 100  # will retry


def test_pushed_changes_append_a_body_addendum(review_run) -> None:
    """Follow-up commits desync the report frozen into the body at publish;
    the addendum marks the body edited so no reader mistakes the original
    report for the current state (maintainer decision 2026-08-09)."""
    root, _bare = review_run
    github = FakeGitHub(comments=[member(101, "the kick looks too aggressive")])
    harness = ResumingHarness(edits={"src/pilot/solvers/tsp.py": "v2 gentler kick\n"})
    outcome = respond(root, github, harness, QueueEvaluator(values=[10.2]))
    assert outcome.action == "replied"
    assert github.body_addenda, "no addendum appended"
    addendum = github.body_addenda[-1]
    assert addendum.startswith("---")
    assert "**Edit (" in addendum and "follow-up" in addendum
    assert "original version" in addendum
    # the measured table is rewritten in place with the re-measured value
    assert github.row_updates == [10.2]


def test_reverted_change_appends_no_addendum(review_run) -> None:
    """The addendum gate is the PUSH, not the attempt: an out-of-scope
    response is reverted, so the body must not claim the solver changed."""
    root, _bare = review_run
    github = FakeGitHub(comments=[member(101, "also update the eval please")])
    harness = ResumingHarness(edits={"docs/roadmap.md": "doctored\n"})
    outcome = respond(root, github, harness)
    assert outcome.action == "replied"
    assert not github.body_addenda  # reverted -> body untouched
    assert not github.row_updates


def test_reply_without_changes_leaves_the_body_alone(review_run) -> None:
    root, _bare = review_run
    github = FakeGitHub(comments=[member(101, "convince me you did not game the eval")])
    outcome = respond(root, github, ResumingHarness())  # no edits -> no push
    assert outcome.action == "replied"
    assert not github.body_addenda


STEWARD_CONTRACT = """\
benchmarks:
  - name: tsp
    command: uv run python -m pilot.eval --env tsp --json
    metric: mean_tour_length
    direction: min
budgets: {gpu_hours_per_run: 1, runs_per_week: 10}
scope: {allowed: [src/pilot/solvers/]}
steward: {allowed: [src/pilot/instances.py, tests/]}
roadmap: docs/roadmap.md
"""


@pytest.fixture
def steward_review_run(tmp_path: Path):
    """An in-review STEWARD run with a retained workspace on a branch."""
    seed = tmp_path / "seed"
    (seed / "src" / "pilot" / "solvers").mkdir(parents=True)
    (seed / "docs").mkdir()
    (seed / "results").mkdir()
    (seed / ".autoresearch.yaml").write_text(STEWARD_CONTRACT)
    (seed / "docs" / "roadmap.md").write_text("# roadmap\n")
    (seed / "src" / "pilot" / "solvers" / "tsp.py").write_text("v1\n")
    (seed / "src" / "pilot" / "instances.py").write_text("SEED = 1\n")
    (seed / "results" / "leader.json").write_text(
        '{"tsp": {"benchmark": "tsp", "metric": "mean_tour_length", "direction": "min",'
        ' "baseline": 14.9, "best": 14.9, "best_run": "baseline-s1", "updated": "2026-08-09"}}\n'
    )
    _git(seed, "init", "-q", "-b", "main")
    _git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    _git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "seed")
    bare = tmp_path / "origin.git"
    _git(tmp_path, "clone", "-q", "--bare", str(seed), str(bare))

    root = tmp_path / "state"
    ws = run_dir(root, "steward-tsp-r1") / "ws"
    ws.parent.mkdir(parents=True)
    _git(tmp_path, "clone", "-q", str(bare), str(ws))
    _git(ws, "switch", "-qc", "feat/steward/steward-01/steward-tsp-r1")

    record = RunRecord(
        run_id="steward-tsp-r1",
        target="org/pilot",
        task_title="steward: tsp",
        benchmark="tsp",
        state=IN_REVIEW,
        agent_id="steward-01",
        pr_url="https://github.com/org/pilot/pull/25",
        resume_session_id="steward-sess",
        last_comment_id=100,
    )
    save_record(root, record, NOW - 1000)
    return root, bare


class StewardEvaluatorFake:
    def __init__(self, value: float):
        self.value = value
        self.checks: list[str] = []

    def check(self, workspace, command) -> None:
        self.checks.append(command)

    def evaluate(self, workspace, command, metric) -> float:
        return self.value


def test_steward_followup_revalidates_and_rebases(steward_review_run) -> None:
    """A comment on a steward PR wakes the STEWARD flow: steward scope on
    the change, the validation ruler (suite runs), and an orchestrator
    re-based row — never the solver's improvement math."""
    root, bare = steward_review_run
    github = FakeGitHub(comments=[member(101, "address the verifier findings")])
    harness = ResumingHarness(edits={"src/pilot/instances.py": "SEED = 'per-run'\n"})
    evaluator = StewardEvaluatorFake(value=15.2)
    outcome = respond_once(
        root,
        "steward-tsp-r1",
        harness,
        evaluator,
        github,  # type: ignore[arg-type]
        bot_login=BOT,
        now=NOW,
        secrets=("sk-x",),
    )
    assert outcome.action == "replied"
    assert "uv run pytest -q" in evaluator.checks  # the steward ruler ran
    leader = _git(bare, "show", "feat/steward/steward-01/steward-tsp-r1:results/leader.json")
    assert '"baseline": 15.2' in leader and '"best": 15.2' in leader  # re-based, not "improved"
    log_msg = _git(bare, "log", "feat/steward/steward-01/steward-tsp-r1", "-1", "--format=%B")
    assert log_msg.startswith("steward: address review feedback")
    assert "Agent: steward-01" in log_msg
    # the resumed session got the steward preamble
    assert "BENCHMARK STEWARD" in harness.calls[0][0]


def test_steward_followup_rejects_solver_territory(steward_review_run) -> None:
    root, bare = steward_review_run
    github = FakeGitHub(comments=[member(101, "please tweak the solver too")])
    harness = ResumingHarness(edits={"src/pilot/solvers/tsp.py": "rigged\n"})
    outcome = respond_once(
        root,
        "steward-tsp-r1",
        harness,
        StewardEvaluatorFake(15.0),
        github,  # type: ignore[arg-type]
        bot_login=BOT,
        now=NOW,
        secrets=(),
    )
    assert outcome.action == "replied"
    assert "outside" in github.posted[0]
    # nothing was pushed at all: the branch never reached the origin
    assert "steward-tsp-r1" not in _git(bare, "branch", "--list")


def test_nonqualifying_comments_ride_as_fenced_context(review_run) -> None:
    """The verifier's findings (no standing) never trigger a wake but DO
    travel in it when a qualifying comment arrives — no human relay."""
    root, _bare = review_run
    verifier_comment = {
        "id": 102,
        "body": "<!-- autoresearch:verification-review -->\nRound 1: caches across calls",
        "user": {"login": "github-actions[bot]"},
        "author_association": "NONE",
    }
    github = FakeGitHub(comments=[verifier_comment, member(103, "address the findings above")])
    harness = ResumingHarness()
    outcome = respond_once(
        root,
        "tsp-r1",
        harness,
        QueueEvaluator(values=[10.5]),
        github,  # type: ignore[arg-type]
        bot_login=BOT,
        now=NOW,
        secrets=(),
    )
    assert outcome.action == "replied"
    prompt = harness.calls[0][0]
    assert "caches across calls" in prompt  # the findings arrived
    assert "context only" in prompt and "not" in prompt  # marked as data
    # a verifier-only thread does NOT wake anyone
    github2 = FakeGitHub(comments=[verifier_comment])
    from autoresearch.followup import has_new_comments
    from autoresearch.runstate import load_record as _lr

    assert not has_new_comments(_lr(root, "tsp-r1"), github2, BOT)  # type: ignore[arg-type]
