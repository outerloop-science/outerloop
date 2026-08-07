"""live_climb end to end: real local git repos, fake harness/evaluator/API."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from autoresearch.climb import live_climb
from autoresearch.harness import SessionResult
from autoresearch.orchestrator import ClimbConfig
from autoresearch.runstate import load_record

CONTRACT = """\
benchmarks:
  - name: tsp
    command: uv run python -m pilot.eval --benchmark tsp --json
    metric: mean_tour_length
    direction: min
budgets: {gpu_hours_per_run: 1, runs_per_week: 10}
scope: {allowed: [src/pilot/solvers/]}
roadmap: docs/roadmap.md
"""


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def target_repo(tmp_path: Path, monkeypatch) -> Path:
    """A bare 'github' repo seeded with a pilot-shaped tree; Workspace.clone
    is monkeypatched to clone from it instead of github.com."""
    seed = tmp_path / "seed"
    (seed / "src" / "pilot" / "solvers").mkdir(parents=True)
    (seed / "docs").mkdir()
    (seed / ".autoresearch.yaml").write_text(CONTRACT)
    (seed / "docs" / "roadmap.md").write_text("# roadmap\n")
    (seed / "src" / "pilot" / "solvers" / "tsp.py").write_text("def solve(): ...\n")
    _git(seed, "init", "-q", "-b", "main")
    _git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    _git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "seed")
    bare = tmp_path / "origin.git"
    _git(tmp_path, "clone", "-q", "--bare", str(seed), str(bare))

    from autoresearch import climb as climb_mod
    from autoresearch.github import Workspace

    real_clone = Workspace.clone

    def fake_clone(url, dest, auth=None, dry_run=False):
        return real_clone(str(bare), dest, auth=None, dry_run=dry_run)

    monkeypatch.setattr(climb_mod.Workspace, "clone", staticmethod(fake_clone))
    return bare


@dataclass
class ScriptedHarness:
    """Applies edits to the workspace like a session would."""

    edits: dict[str, str]
    text: str = "Report: swapped construction heuristic; tours shortened."

    def run(self, brief_text, workspace, resume_session_id=None) -> SessionResult:
        for rel, content in self.edits.items():
            path = workspace / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        return SessionResult(
            stop_reason="end_turn",
            is_error=False,
            cost_usd=0.9,
            num_turns=12,
            session_id="s1",
            final_text=self.text,
            transcript_path="",
        )


@dataclass
class QueueEvaluator:
    values: list[float] = field(default_factory=list)

    def evaluate(self, workspace, command, metric) -> float:
        return self.values.pop(0)


@dataclass
class FakeGitHub:
    prs: list[dict] = field(default_factory=list)
    armed: list[tuple[str, int]] = field(default_factory=list)
    arming_error: str = ""

    def create_pull(self, repo, title, head, base, body, draft=False) -> str:
        self.prs.append(dict(repo=repo, title=title, head=head, base=base, body=body))
        return f"https://github.com/{repo}/pull/1"

    def enable_auto_merge(self, repo, number, method="MERGE") -> None:
        if self.arming_error:
            raise RuntimeError(self.arming_error)
        self.armed.append((repo, number))


@dataclass
class CommentingGitHub(FakeGitHub):
    issue_comments: list = field(default_factory=list)

    def comment(self, repo, number, body):
        self.issue_comments.append((number, body))

    def list_comments(self, repo, number, max_pages=20):
        return []


@dataclass
class NoAuth:
    def token(self) -> str:
        return "unused"


def run_live(tmp_path, target_repo, edits, values, run_id="tsp-1") -> tuple:
    github = FakeGitHub()
    outcome = live_climb(
        config=ClimbConfig(target="org/pilot", benchmark="tsp"),
        run_root=tmp_path / "state",
        run_id=run_id,
        harness=ScriptedHarness(edits=edits),
        evaluator=QueueEvaluator(values=list(values)),
        github=github,  # type: ignore[arg-type]
        bot_auth=NoAuth(),  # type: ignore[arg-type]
        now=1_000_000.0,
        created="2026-08-06T00:00:00Z",
        secrets=("sk-live-key",),
    )
    return outcome, github


def test_improvement_produces_branch_commit_and_pr(tmp_path, target_repo) -> None:
    outcome, github = run_live(
        tmp_path,
        target_repo,
        edits={"src/pilot/solvers/tsp.py": "def solve(): return 'better'\n"},
        values=[13.876, 13.1],
    )
    assert outcome.outcome == "improved"
    assert outcome.pr_url.endswith("/pull/1")
    # the branch landed in the bare origin: the solver edit plus the two
    # orchestrator-written progress files, nothing else
    files = set(
        _git(target_repo, "diff", "--name-only", "main", "feat/auto/agent-01/tsp-1").split()
    )
    assert files == {"src/pilot/solvers/tsp.py", "BENCHMARKS.md", "results/leader.json"}
    pr = github.prs[0]
    assert pr["head"] == "feat/auto/agent-01/tsp-1"
    assert pr["title"] == "[agent] tsp: 13.88 -> 13.1"  # 4 sig figs, not full floats
    assert "measured by the orchestrator" in pr["body"]
    # run record went in-review with the PR url
    record = load_record(tmp_path / "state", "tsp-1")
    assert record.state == "in-review"
    assert "pull/1" in record.ending_note
    # report exists and is redacted-safe
    report = Path(outcome.report_path).read_text()
    assert "improved" in report


def test_no_improvement_ends_negative_result_and_pushes_nothing(tmp_path, target_repo) -> None:
    outcome, github = run_live(
        tmp_path,
        target_repo,
        edits={"src/pilot/solvers/tsp.py": "def solve(): return 'worse'\n"},
        values=[13.876, 14.5],
    )
    assert outcome.outcome == "no-improvement"
    assert github.prs == []
    branches = _git(target_repo, "branch", "--list")
    assert "feat/auto" not in branches
    record = load_record(tmp_path / "state", "tsp-1")
    assert record.state == "ended"
    assert record.ending == "negative-result"


def test_out_of_scope_edit_aborts_without_pr(tmp_path, target_repo) -> None:
    outcome, github = run_live(
        tmp_path,
        target_repo,
        edits={
            "src/pilot/solvers/tsp.py": "def solve(): ...\n",
            "docs/roadmap.md": "doctored\n",
        },
        values=[13.876, 1.0],
    )
    assert outcome.outcome == "scope-violation"
    assert github.prs == []
    record = load_record(tmp_path / "state", "tsp-1")
    assert record.ending == "aborted"
    assert "roadmap" in record.ending_note


def test_session_error_aborts_cleanly(tmp_path, target_repo) -> None:
    @dataclass
    class DeadHarness:
        def run(self, brief_text, workspace, resume_session_id=None) -> SessionResult:
            return SessionResult(
                stop_reason="timeout",
                is_error=True,
                cost_usd=0.0,
                num_turns=0,
                session_id="",
                final_text="",
                transcript_path="",
            )

    github = FakeGitHub()
    outcome = live_climb(
        config=ClimbConfig(target="org/pilot", benchmark="tsp"),
        run_root=tmp_path / "state",
        run_id="tsp-err",
        harness=DeadHarness(),
        evaluator=QueueEvaluator(values=[13.876]),
        github=github,  # type: ignore[arg-type]
        bot_auth=NoAuth(),  # type: ignore[arg-type]
        now=1_000_000.0,
        created="t",
    )
    assert outcome.outcome == "session-error"
    assert github.prs == []


def test_second_run_gets_its_own_branch(tmp_path, target_repo) -> None:
    """A fixed branch name would non-fast-forward on run two."""
    edits = {"src/pilot/solvers/tsp.py": "def solve(): return 1\n"}
    run_live(tmp_path, target_repo, edits=edits, values=[13.876, 13.1], run_id="tsp-a")
    edits2 = {"src/pilot/solvers/tsp.py": "def solve(): return 2\n"}
    outcome, _ = run_live(tmp_path, target_repo, edits=edits2, values=[13.1, 12.5], run_id="tsp-b")
    assert outcome.outcome == "improved"
    branches = _git(target_repo, "branch", "--list")
    assert "feat/auto/agent-01/tsp-a" in branches
    assert "feat/auto/agent-01/tsp-b" in branches


def test_files_written_during_eval_void_the_claim(tmp_path, target_repo) -> None:
    """The committed tree must be exactly the measured tree — solver code
    that writes files at eval time is neither scope-checked nor measured."""

    @dataclass
    class PlantingEvaluator:
        values: list[float] = field(default_factory=list)
        calls: int = 0

        def evaluate(self, workspace, command, metric) -> float:
            self.calls += 1
            if self.calls == 2:  # during the candidate eval
                (workspace / "src" / "pilot" / "solvers" / "planted.py").write_text("x=1\n")
            return self.values.pop(0)

    github = FakeGitHub()
    outcome = live_climb(
        config=ClimbConfig(target="org/pilot", benchmark="tsp"),
        run_root=tmp_path / "state",
        run_id="tsp-drift",
        harness=ScriptedHarness(edits={"src/pilot/solvers/tsp.py": "y=2\n"}),
        evaluator=PlantingEvaluator(values=[13.876, 13.1]),
        github=github,  # type: ignore[arg-type]
        bot_auth=NoAuth(),  # type: ignore[arg-type]
        now=1_000_000.0,
        created="t",
    )
    assert outcome.outcome == "publish-error"
    assert github.prs == []
    assert "feat/auto" not in _git(target_repo, "branch", "--list")
    record = load_record(tmp_path / "state", "tsp-drift")
    assert record.ending == "aborted"
    assert "changed during eval" in record.ending_note


def test_create_pull_failure_records_aborted_not_crash(tmp_path, target_repo) -> None:
    @dataclass
    class FailingGitHub:
        def create_pull(self, *a, **k) -> str:
            raise RuntimeError("422 already exists")

    outcome = live_climb(
        config=ClimbConfig(target="org/pilot", benchmark="tsp"),
        run_root=tmp_path / "state",
        run_id="tsp-prfail",
        harness=ScriptedHarness(edits={"src/pilot/solvers/tsp.py": "z=3\n"}),
        evaluator=QueueEvaluator(values=[13.876, 13.1]),
        github=FailingGitHub(),  # type: ignore[arg-type]
        bot_auth=NoAuth(),  # type: ignore[arg-type]
        now=1_000_000.0,
        created="t",
    )
    assert outcome.outcome == "publish-error"
    record = load_record(tmp_path / "state", "tsp-prfail")
    assert record.state == "ended"
    assert record.ending == "aborted"


def test_report_is_written_for_every_outcome(tmp_path, target_repo) -> None:
    outcome, _ = run_live(
        tmp_path,
        target_repo,
        edits={"src/pilot/solvers/tsp.py": "x = 1\n"},
        values=[13.876, 14.0],
    )
    text = Path(outcome.report_path).read_text()
    assert "no-improvement" in text
    assert "Agent's report" in text


def test_improvement_pr_carries_progress_table(tmp_path, target_repo) -> None:
    """The human-readable record lands in the same PR as the improvement."""
    import json as _json

    outcome, _ = run_live(
        tmp_path,
        target_repo,
        edits={"src/pilot/solvers/tsp.py": "def solve(): return 'better'\n"},
        values=[13.876, 13.1],
    )
    assert outcome.outcome == "improved"
    files = set(
        _git(target_repo, "diff", "--name-only", "main", "feat/auto/agent-01/tsp-1").split()
    )
    assert files == {"src/pilot/solvers/tsp.py", "BENCHMARKS.md", "results/leader.json"}
    table = _git(target_repo, "show", "feat/auto/agent-01/tsp-1:BENCHMARKS.md")
    assert "| tsp | `mean_tour_length` ↓ | 13.876 | 13.1 |" in table
    assert "▲" in table  # improvement marked good even though direction=min
    leader = _json.loads(_git(target_repo, "show", "feat/auto/agent-01/tsp-1:results/leader.json"))
    assert leader["tsp"]["best"] == 13.1
    assert leader["tsp"]["baseline"] == 13.876


def test_agent_editing_benchmarks_md_ends_the_run(tmp_path, target_repo) -> None:
    outcome, github = run_live(
        tmp_path,
        target_repo,
        edits={
            "src/pilot/solvers/tsp.py": "ok\n",
            "BENCHMARKS.md": "| fake | glory |\n",
        },
        values=[13.876, 13.1],
    )
    assert outcome.outcome == "scope-violation"
    assert github.prs == []


def test_second_improvement_updates_best_keeps_baseline(tmp_path, target_repo) -> None:
    import json as _json

    edits = {"src/pilot/solvers/tsp.py": "v1\n"}
    run_live(tmp_path, target_repo, edits=edits, values=[13.876, 13.1], run_id="tsp-a")
    # simulate the merge of run a: advance main to its branch
    _git(target_repo, "branch", "-f", "main", "feat/auto/agent-01/tsp-a")
    edits2 = {"src/pilot/solvers/tsp.py": "v2\n"}
    outcome, _ = run_live(tmp_path, target_repo, edits=edits2, values=[13.1, 12.4], run_id="tsp-b")
    assert outcome.outcome == "improved"
    leader = _json.loads(_git(target_repo, "show", "feat/auto/agent-01/tsp-b:results/leader.json"))
    assert leader["tsp"]["baseline"] == 13.876  # pinned from the FIRST run
    assert leader["tsp"]["best"] == 12.4
    assert leader["tsp"]["best_run"] == "tsp-b"


def test_content_rewrite_during_eval_voids_the_claim(tmp_path, target_repo) -> None:
    """Same path set, different bytes: the fingerprint must catch it."""

    @dataclass
    class RewritingEvaluator:
        values: list[float] = field(default_factory=list)
        calls: int = 0

        def evaluate(self, workspace, command, metric) -> float:
            self.calls += 1
            if self.calls == 2:  # during candidate eval: rewrite the SAME file
                (workspace / "src" / "pilot" / "solvers" / "tsp.py").write_text(
                    "def solve(): return 'unmeasured bytes'\n"
                )
            return self.values.pop(0)

    github = FakeGitHub()
    outcome = live_climb(
        config=ClimbConfig(target="org/pilot", benchmark="tsp"),
        run_root=tmp_path / "state",
        run_id="tsp-rewrite",
        harness=ScriptedHarness(edits={"src/pilot/solvers/tsp.py": "def solve(): return 1\n"}),
        evaluator=RewritingEvaluator(values=[13.876, 13.1]),
        github=github,  # type: ignore[arg-type]
        bot_auth=NoAuth(),  # type: ignore[arg-type]
        now=1_000_000.0,
        created="t",
    )
    assert outcome.outcome == "publish-error"
    assert github.prs == []
    record = load_record(tmp_path / "state", "tsp-rewrite")
    assert "different bytes" in record.ending_note


def test_zero_change_improvement_is_rejected(tmp_path, target_repo) -> None:
    """Metric noise with no edits must never open a PR."""
    outcome, github = run_live(
        tmp_path, target_repo, edits={}, values=[13.876, 13.1], run_id="tsp-noop"
    )
    assert outcome.outcome == "publish-error"
    assert github.prs == []
    assert "zero code changes" in load_record(tmp_path / "state", "tsp-noop").ending_note


def test_climb_once_exception_records_aborted(tmp_path, target_repo) -> None:
    """An unknown benchmark (or any raise) must not strand 'implementing'."""
    github = FakeGitHub()
    outcome = live_climb(
        config=ClimbConfig(target="org/pilot", benchmark="chess"),
        run_root=tmp_path / "state",
        run_id="chess-1",
        harness=ScriptedHarness(edits={}),
        evaluator=QueueEvaluator(values=[1.0]),
        github=github,  # type: ignore[arg-type]
        bot_auth=NoAuth(),  # type: ignore[arg-type]
        now=1_000_000.0,
        created="t",
    )
    assert outcome.outcome == "climb-error"
    record = load_record(tmp_path / "state", "chess-1")
    assert record.state == "ended"
    assert record.ending == "aborted"
    assert "not in contract" in record.ending_note


def test_branch_is_kept_and_recorded_after_pr_failure(tmp_path, target_repo) -> None:
    """create_pull failing does NOT prove no PR exists — the pushed branch is
    left alone (deleting could close a real PR) and recorded for a sweeper."""

    @dataclass
    class FailingGitHub2:
        def create_pull(self, *a, **k) -> str:
            raise RuntimeError("boom")

    live_climb(
        config=ClimbConfig(target="org/pilot", benchmark="tsp"),
        run_root=tmp_path / "state",
        run_id="tsp-orphan",
        harness=ScriptedHarness(edits={"src/pilot/solvers/tsp.py": "q=4\n"}),
        evaluator=QueueEvaluator(values=[13.876, 13.1]),
        github=FailingGitHub2(),  # type: ignore[arg-type]
        bot_auth=NoAuth(),  # type: ignore[arg-type]
        now=1_000_000.0,
        created="t",
    )
    assert "tsp-orphan" in _git(target_repo, "branch", "--list")
    record = load_record(tmp_path / "state", "tsp-orphan")
    assert "branch left on remote: feat/auto/agent-01/tsp-orphan" in record.ending_note


def test_not_beating_recorded_best_is_rejected_loudly(tmp_path, target_repo) -> None:
    """Improved vs a stale baseline but worse than the ledger's best: no PR,
    and the reason is recorded."""
    import json as _json

    # seed a leader in the origin whose best is better than this run's candidate
    seed = tmp_path / "leaderseed"
    _git(tmp_path, "clone", "-q", str(target_repo), str(seed))
    (seed / "results").mkdir(exist_ok=True)
    (seed / "results" / "leader.json").write_text(
        _json.dumps(
            {
                "tsp": {
                    "benchmark": "tsp",
                    "metric": "mean_tour_length",
                    "direction": "min",
                    "baseline": 13.876,
                    "best": 12.0,
                    "best_run": "r0",
                    "updated": "d",
                }
            }
        )
    )
    _git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    _git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "leader")
    _git(seed, "push", "-q", "origin", "main")

    outcome, github = run_live(
        tmp_path,
        target_repo,
        edits={"src/pilot/solvers/tsp.py": "w=9\n"},
        values=[13.876, 13.1],  # improved vs own baseline, worse than best 12.0
        run_id="tsp-stale",
    )
    assert outcome.outcome == "publish-error"
    assert github.prs == []
    assert (
        "does not beat the recorded best"
        in load_record(tmp_path / "state", "tsp-stale").ending_note
    )


def test_climb_error_still_writes_a_report(tmp_path, target_repo) -> None:
    github = FakeGitHub()
    outcome = live_climb(
        config=ClimbConfig(target="org/pilot", benchmark="chess"),
        run_root=tmp_path / "state",
        run_id="chess-2",
        harness=ScriptedHarness(edits={}),
        evaluator=QueueEvaluator(values=[1.0]),
        github=github,  # type: ignore[arg-type]
        bot_auth=NoAuth(),  # type: ignore[arg-type]
        now=1_000_000.0,
        created="t",
    )
    assert outcome.outcome == "climb-error"
    assert Path(outcome.report_path).read_text().startswith("# Run report")


def test_title_pair_never_renders_identical() -> None:
    from autoresearch.climb import _title_pair

    assert _title_pair(13.875696168157484, 10.844662077277105) == "13.88 -> 10.84"
    assert _title_pair(10.00001, 10.00002) == "10.00001 -> 10.00002"
    assert " -> " in _title_pair(1e-7, 2e-7)


def test_issue_run_references_issue_and_reports_back(tmp_path, target_repo) -> None:
    """The requested lane's visible loop: claim → Addresses #N → report."""
    github = CommentingGitHub()
    outcome = live_climb(
        config=ClimbConfig(target="org/pilot", benchmark="tsp"),
        run_root=tmp_path / "state",
        run_id="tsp-iss",
        harness=ScriptedHarness(edits={"src/pilot/solvers/tsp.py": "i=1\n"}),
        evaluator=QueueEvaluator(values=[13.876, 13.1]),
        github=github,  # type: ignore[arg-type]
        bot_auth=NoAuth(),  # type: ignore[arg-type]
        now=1_000_000.0,
        created="2026-08-07T00:00:00Z",
        issue_number=42,
        task_hypothesis="A maintainer asked: make tsp faster (fenced text here)",
    )
    assert outcome.outcome == "improved"
    assert "Addresses #42." in github.prs[0]["body"]
    claim, report = github.issue_comments[0], github.issue_comments[-1]
    assert claim[0] == 42 and "autoresearch:claimed" in claim[1]
    assert report[0] == 42 and "finished (improved)" in report[1]
    assert "pull/1" in report[1]
    record = load_record(tmp_path / "state", "tsp-iss")
    assert record.issue_number == 42


def test_clone_crash_ends_record_and_reports_to_issue(tmp_path, monkeypatch) -> None:
    """A crash BEFORE the contained call (clone/contract/claim) must end the
    record and surface on the issue — not strand `implementing`."""
    from autoresearch import climb as climb_mod

    def exploding_clone(url, dest, auth=None, dry_run=False):
        raise OSError(122, "Disk quota exceeded")

    monkeypatch.setattr(climb_mod.Workspace, "clone", staticmethod(exploding_clone))
    github = CommentingGitHub()
    outcome = live_climb(
        config=ClimbConfig(target="org/pilot", benchmark="tsp"),
        run_root=tmp_path / "state",
        run_id="tsp-clonefail",
        harness=ScriptedHarness(edits={}),
        evaluator=QueueEvaluator(values=[]),
        github=github,  # type: ignore[arg-type]
        bot_auth=NoAuth(),  # type: ignore[arg-type]
        now=1_000_000.0,
        created="t",
        issue_number=7,
    )
    assert outcome.outcome == "climb-error"
    record = load_record(tmp_path / "state", "tsp-clonefail")
    assert record.state == "ended" and record.ending == "aborted"
    assert "quota" in record.ending_note
    assert any("climb-error" in body for _, body in github.issue_comments)
    # exception DETAIL stays local: redact() only knows the secrets tuple,
    # so raw messages (paths, embedded tokens) never reach the public issue
    assert not any("quota" in body for _, body in github.issue_comments)


def _save_failing_after_first(monkeypatch):
    """save_record succeeds once (the implementing record) then raises — the
    quota-crisis failure mode where the ENDING write is what dies."""
    from autoresearch import climb as climb_mod

    real_save = climb_mod.save_record
    calls = {"n": 0}

    def failing(root, record, now):
        calls["n"] += 1
        if calls["n"] > 1:
            raise OSError(122, "Disk quota exceeded")
        real_save(root, record, now)

    monkeypatch.setattr(climb_mod, "save_record", failing)


def test_ending_steps_degrade_independently(tmp_path, target_repo, monkeypatch) -> None:
    """A full disk must not block the GitHub failure report (the 2026-08-07
    stranding: the ending record raised EDQUOT inside the handler and took
    the report and issue post down with it)."""
    _save_failing_after_first(monkeypatch)
    github = CommentingGitHub()
    outcome = live_climb(
        config=ClimbConfig(target="org/pilot", benchmark="chess"),  # not in contract
        run_root=tmp_path / "state",
        run_id="chess-disk",
        harness=ScriptedHarness(edits={}),
        evaluator=QueueEvaluator(values=[1.0]),
        github=github,  # type: ignore[arg-type]
        bot_auth=NoAuth(),  # type: ignore[arg-type]
        now=1_000_000.0,
        created="t",
        issue_number=7,
    )
    assert outcome.outcome == "climb-error"  # returned, never raised
    # the record could not be ended (disk dead) — but the failure is VISIBLE:
    assert any("climb-error" in body for _, body in github.issue_comments)
    assert Path(outcome.report_path).exists()
    assert not any("not in contract" in body for _, body in github.issue_comments)


def test_final_record_failure_does_not_lose_pr_or_issue_report(
    tmp_path, target_repo, monkeypatch
) -> None:
    """An improvement whose FINAL record save dies must still open the PR and
    report back to the issue."""
    _save_failing_after_first(monkeypatch)
    github = CommentingGitHub()
    outcome = live_climb(
        config=ClimbConfig(target="org/pilot", benchmark="tsp"),
        run_root=tmp_path / "state",
        run_id="tsp-finaldisk",
        harness=ScriptedHarness(edits={"src/pilot/solvers/tsp.py": "z=1\n"}),
        evaluator=QueueEvaluator(values=[13.876, 13.1]),
        github=github,  # type: ignore[arg-type]
        bot_auth=NoAuth(),  # type: ignore[arg-type]
        now=1_000_000.0,
        created="2026-08-07T00:00:00Z",
        issue_number=9,
    )
    assert outcome.outcome == "improved"
    assert github.prs and outcome.pr_url.endswith("/pull/1")
    assert any("finished (improved)" in body for _, body in github.issue_comments)
    # the un-saveable record means follow-up servicing is blind to this PR —
    # the warning lands where the humans are looking
    assert any(
        num == 1 and "follow-up servicing is offline" in body for num, body in github.issue_comments
    )


def test_first_record_write_failure_is_contained(tmp_path, target_repo, monkeypatch) -> None:
    """If not even the initial record can be written, the run must not
    proceed invisibly OR crash the caller: climb-error plus an issue post."""
    from autoresearch import climb as climb_mod

    def always_failing(root, record, now):
        raise OSError(122, "Disk quota exceeded")

    monkeypatch.setattr(climb_mod, "save_record", always_failing)
    github = CommentingGitHub()
    outcome = live_climb(
        config=ClimbConfig(target="org/pilot", benchmark="tsp"),
        run_root=tmp_path / "state",
        run_id="tsp-recfail",
        harness=ScriptedHarness(edits={}),
        evaluator=QueueEvaluator(values=[]),
        github=github,  # type: ignore[arg-type]
        bot_auth=NoAuth(),  # type: ignore[arg-type]
        now=1_000_000.0,
        created="t",
        issue_number=7,
    )
    assert outcome.outcome == "climb-error"
    assert outcome.report_path == ""  # never point at a report that was not written
    assert any("could not start" in body for _, body in github.issue_comments)


def test_improvement_arms_auto_merge(tmp_path, target_repo) -> None:
    """Publish hands the merge to the human approval: auto-merge is armed on
    the fresh PR, so approving is the last human action needed."""
    outcome, github = run_live(
        tmp_path,
        target_repo,
        edits={"src/pilot/solvers/tsp.py": "am=1\n"},
        values=[13.876, 13.1],
        run_id="tsp-arm",
    )
    assert outcome.outcome == "improved"
    assert github.armed == [("org/pilot", 1)]


def test_arming_failure_never_fails_the_publish(tmp_path, target_repo) -> None:
    """Repos without auto-merge enabled refuse the mutation; the PR must
    survive that (arming is convenience, not correctness)."""
    github = FakeGitHub(arming_error="auto merge is not allowed")
    outcome = live_climb(
        config=ClimbConfig(target="org/pilot", benchmark="tsp"),
        run_root=tmp_path / "state",
        run_id="tsp-noarm",
        harness=ScriptedHarness(edits={"src/pilot/solvers/tsp.py": "na=2\n"}),
        evaluator=QueueEvaluator(values=[13.876, 13.1]),
        github=github,  # type: ignore[arg-type]
        bot_auth=NoAuth(),  # type: ignore[arg-type]
        now=1_000_000.0,
        created="t",
    )
    assert outcome.outcome == "improved"
    assert outcome.pr_url.endswith("/pull/1")
    assert github.armed == []
