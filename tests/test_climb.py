"""live_climb end to end: real local git repos, fake harness/evaluator/API."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from autoresearch.climb import _park_run, live_climb
from autoresearch.dispatch import Snapshot
from autoresearch.harness import SessionResult
from autoresearch.orchestrator import ClimbConfig, ClimbParked
from autoresearch.runstate import RunRecord, load_record

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


def _session(sid: str = "s1") -> SessionResult:
    return SessionResult(
        stop_reason="end_turn",
        is_error=False,
        cost_usd=1.0,
        num_turns=5,
        session_id=sid,
        final_text="report",
        transcript_path="",
    )


def test_park_run_writes_a_waiting_record_with_the_reentry_stage(tmp_path) -> None:
    record = RunRecord(
        run_id="tsp-1", target="org/pilot", task_title="t", state="implementing", benchmark="tsp"
    )
    snap = Snapshot(commit="c" * 40, tree="d" * 40, ref="refs/dispatch/tok")
    parked = ClimbParked(
        phase="candidate",
        afterany="afterany:101:102",
        base_sha="b" * 40,
        seed=7,
        suite_seed=9,
        candidate_sha="c" * 40,
        session=_session("s1"),
    )
    _park_run(tmp_path, record, parked, snap.ref, eval_minutes=90, now=1000.0)

    r = load_record(tmp_path, "tsp-1")
    assert r.state == "waiting"
    # a MULTI-job park records no single experiment job — the sweep must not
    # wake when job 101 finishes while 102 runs; it rides the deadline floor.
    assert r.experiment_job_id == ""
    # deadline is walltime-aware (eval walltime + queue slack), not a flat 24h,
    # so the sweep never cancels a still-queued job of a legitimately slow eval
    assert r.deadline == 1000.0 + (90 + 12 * 60) * 60
    assert r.terminal_seen == 0.0  # the NEW experiment has not been seen terminal
    assert r.resume_session_id == "s1"  # the candidate park resumes the session
    assert r.stage["phase"] == "candidate"
    assert r.stage["base_sha"] == "b" * 40 and r.stage["candidate_sha"] == "c" * 40
    assert r.stage["candidate_ref"] == "refs/dispatch/tok"  # for drop at the terminal
    assert r.stage["seed"] == 7 and r.stage["suite_seed"] == 9
    assert r.stage["afterany"] == "afterany:101:102"


def test_park_run_single_job_records_it_for_the_sweep(tmp_path) -> None:
    # one eval job (a baseline park, or a candidate with no siblings): the sweep
    # CAN poll it directly for a terminal+grace wake.
    record = RunRecord(
        run_id="tsp-3", target="org/pilot", task_title="t", state="implementing", benchmark="tsp"
    )
    parked = ClimbParked(
        phase="baseline", afterany="afterany:77", base_sha="b" * 40, seed=0, suite_seed=0
    )
    _park_run(tmp_path, record, parked, "", eval_minutes=90, now=1000.0)
    assert load_record(tmp_path, "tsp-3").experiment_job_id == "77"


def test_park_resets_wake_attempts_a_productive_park_left_waiting(tmp_path) -> None:
    # the run reached IMPLEMENTING (left waiting, did work) before this park, so
    # "wakes since it last left waiting" resets — a productive park/wake cycle
    # must not creep toward the stuck cap.
    record = RunRecord(
        run_id="tsp-4",
        target="org/pilot",
        task_title="t",
        state="implementing",
        benchmark="tsp",
        wake_attempts=2,
    )
    parked = ClimbParked(
        phase="baseline", afterany="afterany:9", base_sha="b" * 40, seed=0, suite_seed=0
    )
    _park_run(tmp_path, record, parked, "", eval_minutes=90, now=1000.0)
    assert load_record(tmp_path, "tsp-4").wake_attempts == 0


def test_park_run_baseline_phase_has_no_candidate_or_session(tmp_path) -> None:
    record = RunRecord(
        run_id="tsp-2", target="org/pilot", task_title="t", state="implementing", benchmark="tsp"
    )
    parked = ClimbParked(
        phase="baseline", afterany="afterany:55", base_sha="b" * 40, seed=0, suite_seed=0
    )
    _park_run(tmp_path, record, parked, "", eval_minutes=90, now=1000.0)

    r = load_record(tmp_path, "tsp-2")
    assert r.state == "waiting" and r.resume_session_id == ""  # session not run yet
    assert r.stage["phase"] == "baseline"
    assert r.stage["candidate_sha"] == "" and r.stage["candidate_ref"] == ""


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
    seen_dirs: list = field(default_factory=list)

    def evaluate(self, workspace, command, metric, extra_env=None) -> float:
        self.seen_dirs.append(Path(workspace))
        return self.values.pop(0)


@dataclass
class FakeGitHub:
    prs: list[dict] = field(default_factory=list)
    armed: list[tuple[str, int]] = field(default_factory=list)
    arming_error: str = ""

    def create_pull(self, repo, title, head, base, body, draft=False) -> str:
        self.prs.append(dict(repo=repo, title=title, head=head, base=base, body=body, draft=draft))
        return f"https://github.com/{repo}/pull/1"

    def arm_auto_merge_when_review_required(self, repo, number) -> bool:
        if self.arming_error:
            raise RuntimeError(self.arming_error)
        self.armed.append((repo, number))
        return True


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


def test_snapshot_refs_are_dropped_after_a_climb(tmp_path, target_repo) -> None:
    # the candidate snapshots are retained by ref during measurement; the climb
    # must drop every one when it ends, or each parked-or-finished run leaks a
    # ref and its commit (terra's #102 round-9 concern, now enforced in code).
    outcome, _ = run_live(
        tmp_path,
        target_repo,
        edits={"src/pilot/solvers/tsp.py": "def solve(): return 'better'\n"},
        values=[13.876, 13.1],
    )
    # an improved outcome means a candidate was measured, which REQUIRES a
    # snapshot — so refs-empty here proves dropped, not never-created.
    assert outcome.outcome == "improved"
    ws = tmp_path / "state" / "runs" / "tsp-1" / "ws"
    assert _git(ws, "for-each-ref", "refs/dispatch/").strip() == ""


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
                stop_reason="spawn-error",
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


def test_exhausted_live_climb_ends_budget_exhausted(tmp_path, target_repo) -> None:
    """The session-budget outcome flows through the ending map on a LIVE
    climb: the record says budget-exhausted with the real cause."""

    @dataclass
    class DryHarness:
        def run(self, brief_text, workspace, resume_session_id=None) -> SessionResult:
            return SessionResult(
                stop_reason="tool_use",
                is_error=True,
                cost_usd=2.0,
                num_turns=120,
                session_id="",
                final_text="",
                transcript_path="",
                error_detail="error_max_turns: Reached maximum number of turns (120)",
            )

    outcome = live_climb(
        config=ClimbConfig(target="org/pilot", benchmark="tsp"),
        run_root=tmp_path / "state",
        run_id="tsp-dry",
        harness=DryHarness(),
        evaluator=QueueEvaluator(values=[13.876]),
        github=FakeGitHub(),  # type: ignore[arg-type]
        bot_auth=NoAuth(),  # type: ignore[arg-type]
        now=1_000_000.0,
        created="t",
    )
    assert outcome.outcome == "session-budget"
    record = load_record(tmp_path / "state", "tsp-dry")
    assert record.ending == "budget-exhausted"
    assert "maximum number of turns" in record.ending_note


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

        def evaluate(self, workspace, command, metric, extra_env=None) -> float:
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

        def evaluate(self, workspace, command, metric, extra_env=None) -> float:
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


def _push_contract(tmp_path, target_repo, contract_text: str, name: str) -> None:
    seed = tmp_path / f"contract-{name}"
    _git(tmp_path, "clone", "-q", str(target_repo), str(seed))
    (seed / ".autoresearch.yaml").write_text(contract_text)
    _git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    _git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", name)
    _git(seed, "push", "-q", "origin", "main")


def test_within_noise_floor_is_an_honest_negative(tmp_path, target_repo) -> None:
    """Beats the recorded best, but by less than min_delta: the recorded
    best was measured under a DIFFERENT seed, so the delta is pool luck —
    an honest negative result, never a PR and never an abort."""
    import json as _json

    _push_contract(
        tmp_path,
        target_repo,
        CONTRACT.replace("    direction: min\n", "    direction: min\n    min_delta: 0.5\n", 1),
        "floor",
    )
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
        values=[13.876, 11.8],  # beats best 12.0, but only by 0.2 < 0.5
        run_id="tsp-floor",
    )
    assert outcome.outcome == "no-improvement"
    assert github.prs == []
    record = load_record(tmp_path / "state", "tsp-floor")
    assert record.ending == "negative-result"
    assert "noise floor" in record.ending_note and "0.5" in record.ending_note


def test_sub_threshold_delta_on_floored_benchmark_is_negative_not_abort(
    tmp_path, target_repo
) -> None:
    """Round-4 finding: with min_delta declared, EVERY sub-floor delta over
    the recorded best — including one below the relative threshold — is an
    honest negative, not a stale-clone abort."""
    import json as _json

    _push_contract(
        tmp_path,
        target_repo,
        CONTRACT.replace("    direction: min\n", "    direction: min\n    min_delta: 0.5\n", 1),
        "subrel",
    )
    seed = tmp_path / "leaderseed2"
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
        edits={"src/pilot/solvers/tsp.py": "w=11\n"},
        values=[13.876, 11.999],  # 0.001 over best: below rel threshold AND floor
        run_id="tsp-subrel",
    )
    assert outcome.outcome == "no-improvement"
    assert github.prs == []
    record = load_record(tmp_path / "state", "tsp-subrel")
    assert record.ending == "negative-result"
    assert "noise floor" in record.ending_note


def test_baseline_eval_runs_outside_the_session_workspace(tmp_path, target_repo) -> None:
    """The baseline is measured in a throwaway worktree of the pre-session
    commit: eval artifacts (even gitignored) never land in the tree the
    solver session sees, so a pinned seed cannot leak the pool."""
    github = FakeGitHub()
    evaluator = QueueEvaluator(values=[13.876, 13.1])
    outcome = live_climb(
        config=ClimbConfig(target="org/pilot", benchmark="tsp"),
        run_root=tmp_path / "state",
        run_id="tsp-iso",
        harness=ScriptedHarness(edits={"src/pilot/solvers/tsp.py": "w=5\n"}),
        evaluator=evaluator,
        github=github,  # type: ignore[arg-type]
        bot_auth=NoAuth(),  # type: ignore[arg-type]
        now=1_000_000.0,
        created="t",
    )
    assert outcome.outcome == "improved"
    ws = tmp_path / "state" / "runs" / "tsp-iso" / "ws"
    baseline_dir, candidate_dir = evaluator.seen_dirs
    assert baseline_dir.name == "measure-baseline" and baseline_dir != ws
    assert candidate_dir == ws
    assert not baseline_dir.exists()  # cleaned up with the run


def test_seeded_climb_records_the_seed_in_the_ledger(tmp_path, target_repo) -> None:
    """The ledger row carries the seed the best was measured under: the
    number becomes re-derivable instead of pool luck."""
    import json as _json

    _push_contract(
        tmp_path,
        target_repo,
        CONTRACT.replace(
            "    direction: min\n", "    direction: min\n    seed_env: PILOT_TSP_SEED\n", 1
        ),
        "seeded",
    )
    outcome, _github = run_live(
        tmp_path,
        target_repo,
        edits={"src/pilot/solvers/tsp.py": "w=8\n"},
        values=[13.876, 13.1, 13.876, 13.1],  # climb pair + freshness pair
        run_id="tsp-seeded",
    )
    assert outcome.outcome == "improved"
    leader = _json.loads(
        _git(target_repo, "show", "feat/auto/agent-01/tsp-seeded:results/leader.json")
    )
    assert leader["tsp"]["run_seed"] > 0


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


def _push_upstream(target_repo, tmp_path, rel_path: str, content: str, name: str) -> None:
    """Simulate a concurrent merge: land a commit on the origin's main."""
    side = tmp_path / f"side-{name}"
    _git(tmp_path, "clone", "-q", str(target_repo), str(side))
    p = side / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    _git(side, "-c", "user.name=u", "-c", "user.email=u@u", "add", "-A")
    _git(side, "-c", "user.name=u", "-c", "user.email=u@u", "commit", "-qm", f"upstream {name}")
    _git(side, "push", "-q", "origin", "main")


@dataclass
class RacingHarness(ScriptedHarness):
    """Applies its edits AND lands an upstream commit mid-session."""

    target_repo: object = None
    tmp_path: object = None
    upstream_path: str = "docs/roadmap.md"
    upstream_content: str = "# roadmap moved\n"

    def run(self, brief_text, workspace, resume_session_id=None):
        _push_upstream(
            self.target_repo, self.tmp_path, self.upstream_path, self.upstream_content, "race"
        )
        return super().run(brief_text, workspace, resume_session_id)


@dataclass
class DirCheckingEvaluator(QueueEvaluator):
    """Pops values like QueueEvaluator but also records, per call, whether
    the tree it was handed contained the agent's edit — proving the two
    post-merge measurements ran on the intended PRISTINE trees, not on the
    session's long-lived workspace."""

    saw_agent_edit: list = field(default_factory=list)

    def evaluate(self, workspace, command, metric, extra_env=None) -> float:
        solver = Path(workspace) / "src" / "pilot" / "solvers" / "tsp.py"
        self.saw_agent_edit.append(solver.exists() and "r=1" in solver.read_text())
        return super().evaluate(workspace, command, metric)


def test_moved_base_is_merged_and_remeasured_before_push(tmp_path, target_repo) -> None:
    """Main moves during the climb (disjoint file): the branch merges the
    fresh base (merge commit, never rebase), the claim is RE-MEASURED on the
    merged tree, and the PR lands with both histories."""
    github = FakeGitHub()
    # values: session baseline, session candidate, then the post-merge pair —
    # fresh-base baseline (worktree of FETCH_HEAD) and merged-tree candidate
    evaluator = DirCheckingEvaluator(values=[13.876, 13.1, 13.9, 13.2])
    outcome = live_climb(
        config=ClimbConfig(target="org/pilot", benchmark="tsp"),
        run_root=tmp_path / "state",
        run_id="tsp-race",
        harness=RacingHarness(
            edits={"src/pilot/solvers/tsp.py": "r=1\n"},
            target_repo=target_repo,
            tmp_path=tmp_path,
        ),
        evaluator=evaluator,
        github=github,  # type: ignore[arg-type]
        bot_auth=NoAuth(),  # type: ignore[arg-type]
        now=1_000_000.0,
        created="2026-08-07T00:00:00Z",
    )
    assert outcome.outcome == "improved"
    assert evaluator.values == []  # both post-merge measurements actually ran
    # call 3 = fresh base WITHOUT the agent edit; call 4 = merged tree WITH it
    # (calls 1-2 are the session-time pair in the live workspace)
    assert evaluator.saw_agent_edit[2:] == [False, True]
    # the worktrees were cleaned up (no disk leak in the run dir)
    leftovers = [p.name for p in (tmp_path / "state" / "runs" / "tsp-race").iterdir()]
    assert "measure-fresh-base" not in leftovers and "measure-merged" not in leftovers
    assert github.prs and github.prs[0]["head"] == "feat/auto/agent-01/tsp-race"
    # the PR claims the post-merge pair, not the session-time numbers
    assert "13.9 -> 13.2" in github.prs[0]["title"]
    # a true merge commit landed (never a rebase)
    assert _git(target_repo, "log", "--merges", "--oneline", "feat/auto/agent-01/tsp-race")
    # the branch contains the upstream commit (merged, not ignored)
    branch_files = _git(target_repo, "ls-tree", "-r", "--name-only", "feat/auto/agent-01/tsp-race")
    assert "docs/roadmap.md" in branch_files
    upstream_on_branch = _git(target_repo, "show", "feat/auto/agent-01/tsp-race:docs/roadmap.md")
    assert upstream_on_branch == "# roadmap moved\n"
    # vs the MOVED main, the branch changes only solver + progress files
    files = set(
        _git(target_repo, "diff", "--name-only", "main", "feat/auto/agent-01/tsp-race").split()
    )
    assert files == {"src/pilot/solvers/tsp.py", "BENCHMARKS.md", "results/leader.json"}


def test_conflicting_moved_base_is_publish_error_not_a_broken_pr(tmp_path, target_repo) -> None:
    """Upstream rewrites the SAME file the agent edited: the merge conflicts,
    nothing is pushed, and the run ends honestly instead of opening an
    unmergeable PR."""
    github = FakeGitHub()
    outcome = live_climb(
        config=ClimbConfig(target="org/pilot", benchmark="tsp"),
        run_root=tmp_path / "state",
        run_id="tsp-clash",
        harness=RacingHarness(
            edits={"src/pilot/solvers/tsp.py": "mine=1\n"},
            target_repo=target_repo,
            tmp_path=tmp_path,
            upstream_path="src/pilot/solvers/tsp.py",
            upstream_content="theirs=2\n",
        ),
        evaluator=QueueEvaluator(values=[13.876, 13.1]),
        github=github,  # type: ignore[arg-type]
        bot_auth=NoAuth(),  # type: ignore[arg-type]
        now=1_000_000.0,
        created="t",
    )
    assert outcome.outcome == "publish-error"
    assert github.prs == []
    assert "feat/auto/agent-01/tsp-clash" not in _git(target_repo, "branch", "--list")
    record = load_record(tmp_path / "state", "tsp-clash")
    # pin the TRIAGE branch, not just the topic: this is a content conflict,
    # and must not be reported through the infrastructure-failure message
    assert "conflicted" in record.ending_note
    assert "not a content conflict" not in record.ending_note


def test_absorbed_improvement_after_merge_is_rejected(tmp_path, target_repo) -> None:
    """The merged tree no longer beats the baseline (upstream absorbed the
    win): re-measurement vetoes the push."""
    github = FakeGitHub()
    outcome = live_climb(
        config=ClimbConfig(target="org/pilot", benchmark="tsp"),
        run_root=tmp_path / "state",
        run_id="tsp-absorbed",
        harness=RacingHarness(
            edits={"src/pilot/solvers/tsp.py": "a=1\n"},
            target_repo=target_repo,
            tmp_path=tmp_path,
        ),
        # post-merge: fresh base measures 13.0, merged tree only 13.05 —
        # upstream absorbed the win; the candidate REGRESSES the fresh base
        evaluator=QueueEvaluator(values=[13.876, 13.1, 13.0, 13.05]),
        github=github,  # type: ignore[arg-type]
        bot_auth=NoAuth(),  # type: ignore[arg-type]
        now=1_000_000.0,
        created="t",
    )
    assert outcome.outcome == "publish-error"
    assert github.prs == []
    assert (
        "does not beat the fresh base"
        in load_record(tmp_path / "state", "tsp-absorbed").ending_note
    )


def test_terminated_is_contained_like_any_crash(tmp_path, target_repo) -> None:
    """A SIGTERM surfaced as Terminated mid-session must end the run through
    the ordinary containment: record aborted, report written."""
    from autoresearch.climb import Terminated

    @dataclass
    class KilledHarness(ScriptedHarness):
        def run(self, brief_text, workspace, resume_session_id=None):
            raise Terminated("SIGTERM from Slurm (walltime, preemption, or scancel)")

    github = CommentingGitHub()
    outcome = live_climb(
        config=ClimbConfig(target="org/pilot", benchmark="tsp"),
        run_root=tmp_path / "state",
        run_id="tsp-term",
        harness=KilledHarness(edits={}),
        evaluator=QueueEvaluator(values=[13.876]),
        github=github,  # type: ignore[arg-type]
        bot_auth=NoAuth(),  # type: ignore[arg-type]
        now=1_000_000.0,
        created="t",
        issue_number=7,
    )
    assert outcome.outcome == "climb-error"
    record = load_record(tmp_path / "state", "tsp-term")
    assert record.state == "ended" and record.ending == "aborted"
    assert "SIGTERM" in record.ending_note
    assert any("climb-error" in body for _, body in github.issue_comments)


def test_sigterm_containment_is_one_shot() -> None:
    """First SIGTERM raises Terminated; a second must NOT interrupt the
    containment the first one started."""
    import os
    import signal

    import pytest

    from autoresearch.climb import Terminated, arm_sigterm_containment

    original = signal.getsignal(signal.SIGTERM)
    try:
        arm_sigterm_containment()
        with pytest.raises(Terminated):
            os.kill(os.getpid(), signal.SIGTERM)
        os.kill(os.getpid(), signal.SIGTERM)  # disarmed: must not raise
    finally:
        signal.signal(signal.SIGTERM, original)


def test_climb_job_id_is_stamped_from_slurm_env(tmp_path, target_repo, monkeypatch) -> None:
    """The sweep's entire kill-detection keys on this field: the record must
    carry the climb's own SLURM_JOB_ID."""
    monkeypatch.setenv("SLURM_JOB_ID", "4242")
    run_live(
        tmp_path,
        target_repo,
        edits={"src/pilot/solvers/tsp.py": "jid=1\n"},
        values=[13.876, 13.1],
        run_id="tsp-jid",
    )
    assert load_record(tmp_path / "state", "tsp-jid").climb_job_id == "4242"


def test_self_deadline_arms_before_the_walltime(monkeypatch) -> None:
    """The alarm fires margin seconds before the job's walltime — our only
    pre-kill warning on clusters that never signal our process. Hermetic:
    inside a real allocation SLURM_JOB_START_TIME would change the math."""
    import signal

    from autoresearch.climb import arm_self_deadline

    monkeypatch.delenv("SLURM_JOB_START_TIME", raising=False)
    original = signal.getsignal(signal.SIGALRM)
    try:
        armed = arm_self_deadline(90, margin_s=120.0)
        assert armed == 90 * 60 - 120
        assert signal.alarm(0) > 0  # a real alarm was pending
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, original)


def test_self_deadline_anchors_on_slurm_job_start(monkeypatch) -> None:
    """With SLURM_JOB_START_TIME set, startup latency erodes the runway,
    never the margin: a job already 10 minutes in arms 10 minutes less."""
    import signal
    import time

    from autoresearch.climb import arm_self_deadline

    monkeypatch.setenv("SLURM_JOB_START_TIME", str(int(time.time()) - 600))
    original = signal.getsignal(signal.SIGALRM)
    try:
        armed = arm_self_deadline(90, margin_s=120.0)
        assert abs(armed - (90 * 60 - 600 - 120)) <= 2
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, original)


def test_self_deadline_margin_floor_and_off_switch(monkeypatch) -> None:
    import signal

    from autoresearch.climb import MIN_ARM_S, arm_self_deadline

    monkeypatch.delenv("SLURM_JOB_START_TIME", raising=False)
    original = signal.getsignal(signal.SIGALRM)
    try:
        assert arm_self_deadline(0) == 0  # off
        assert arm_self_deadline(1, margin_s=1.0) == 0  # walltime <= floored margin
        assert arm_self_deadline(3, margin_s=1.0) == 0  # 120s runway < 180s floor
        armed = arm_self_deadline(10, margin_s=1.0)
        assert armed == 10 * 60 - 60 and armed >= MIN_ARM_S  # margin floor 60
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, original)


def test_self_deadline_raises_terminated_into_containment(monkeypatch) -> None:
    import signal

    import pytest

    from autoresearch.climb import Terminated, arm_self_deadline

    monkeypatch.delenv("SLURM_JOB_START_TIME", raising=False)
    original = signal.getsignal(signal.SIGALRM)
    try:
        arm_self_deadline(90)
        handler = signal.getsignal(signal.SIGALRM)
        assert callable(handler)
        with pytest.raises(Terminated, match="self-deadline"):
            handler(signal.SIGALRM, None)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, original)


def test_moved_base_regates_the_suite_on_the_merged_tree(tmp_path, target_repo) -> None:
    """The suite gate must hold on the tree that actually lands: the sibling
    passed at session time, but the merged tree regresses it — re-measurement
    vetoes the push, same as an absorbed improvement."""
    _push_contract(
        tmp_path,
        target_repo,
        CONTRACT.replace(
            "budgets:",
            "  - name: sokoban\n"
            "    command: uv run python -m pilot.eval --benchmark sokoban --json\n"
            "    metric: solve_rate\n"
            "    direction: max\n"
            "budgets:",
        ).replace(
            "scope: {allowed: [src/pilot/solvers/]}",
            "scope: {allowed: [src/pilot/], shared: [src/pilot/model/]}",
        ),
        "suite",
    )
    github = FakeGitHub()
    outcome = live_climb(
        config=ClimbConfig(target="org/pilot", benchmark="tsp"),
        run_root=tmp_path / "state",
        run_id="tsp-suite-race",
        harness=RacingHarness(
            edits={"src/pilot/model/encoder.py": "shared=1\n"},
            target_repo=target_repo,
            tmp_path=tmp_path,
        ),
        # session pair, sibling pair (passes: 0.8 flat), then post-merge:
        # climbed pair still improves, but the merged tree's sibling drops
        evaluator=QueueEvaluator(values=[13.876, 13.1, 0.8, 0.8, 13.9, 13.2, 0.8, 0.5]),
        github=github,  # type: ignore[arg-type]
        bot_auth=NoAuth(),  # type: ignore[arg-type]
        now=1_000_000.0,
        created="t",
    )
    assert outcome.outcome == "suite-regression"
    assert github.prs == []
    record = load_record(tmp_path / "state", "tsp-suite-race")
    assert record.ending == "negative-result"  # the gate's promise: never an abort
    assert "suite regression after merging" in record.ending_note
    assert "sokoban" in record.ending_note


def test_author_harness_is_built_from_the_spec() -> None:
    """The spec is the single source for the session budget: manifest and
    harness cannot disagree (the judges' build_reviewer_harness pattern)."""
    from autoresearch.climb import build_editor_harness
    from autoresearch.roles import author_spec, reviewer_spec

    spec = author_spec(max_turns=7, walltime_s=120)
    harness = build_editor_harness("sk-key", spec, container_image="img.sif")
    assert harness.max_turns == 7
    assert harness.timeout_s == 120
    assert harness.container_image == "img.sif"
    with pytest.raises(ValueError, match="editing roles"):
        build_editor_harness("sk-key", reviewer_spec())


def _panel_judge(texts):
    """A scripted read-only judge for the real run_panel path."""
    from dataclasses import dataclass, field

    @dataclass
    class _J:
        queue: list = field(default_factory=lambda: list(texts))
        seen_ws: list = field(default_factory=list)

        def run(self, brief_text, workspace, resume_session_id=None) -> SessionResult:
            self.seen_ws.append(Path(workspace))
            return SessionResult(
                stop_reason="end_turn",
                is_error=False,
                cost_usd=0.0,
                num_turns=1,
                session_id="judge",
                final_text=self.queue.pop(0),
                transcript_path="",
            )

    return _J()


def test_panel_clean_read_lands_a_normal_pr_with_transcript(tmp_path, target_repo) -> None:
    import json as _json

    from autoresearch.panel import PanelLens

    judge = _panel_judge([_json.dumps({"findings": [], "notes": "clean"})])
    github = FakeGitHub()
    outcome = live_climb(
        config=ClimbConfig(target="org/pilot", benchmark="tsp"),
        run_root=tmp_path / "state",
        run_id="tsp-panel-ok",
        harness=ScriptedHarness(edits={"src/pilot/solvers/tsp.py": "p=1\n"}),
        evaluator=QueueEvaluator(values=[13.876, 13.1]),
        github=github,  # type: ignore[arg-type]
        bot_auth=NoAuth(),  # type: ignore[arg-type]
        now=1_000_000.0,
        created="2026-08-15T00:00:00Z",
        panel_lenses=(PanelLens("review", judge),),
    )
    assert outcome.outcome == "improved"
    pr = github.prs[0]
    assert pr["draft"] is False
    assert "## Pre-PR verification" in pr["body"]
    assert "0 blocking" in pr["body"]
    # the judge read a SANITIZED candidate worktree, not the live workspace
    assert judge.seen_ws[0].name == "pr-head"
    # panel worktrees cleaned up
    leftovers = [p.name for p in (tmp_path / "state" / "runs" / "tsp-panel-ok").iterdir()]
    assert "panel" not in leftovers
    assert github.armed  # clean panel: auto-merge arming still runs


def test_panel_capped_blocking_opens_a_draft_and_never_arms(tmp_path, target_repo) -> None:
    import json as _json

    from autoresearch.panel import PanelLens

    blocking = _json.dumps(
        {
            "findings": [
                {
                    "file": "src/pilot/solvers/tsp.py",
                    "line": 1,
                    "confidence": "high",
                    "summary": "suspicious lever",
                    "detail": "looks structural",
                    "blocking": True,
                }
            ],
            "notes": "",
        }
    )
    judge = _panel_judge([blocking, blocking])
    github = FakeGitHub()
    outcome = live_climb(
        config=ClimbConfig(target="org/pilot", benchmark="tsp"),
        run_root=tmp_path / "state",
        run_id="tsp-panel-draft",
        harness=ScriptedHarness(edits={"src/pilot/solvers/tsp.py": "p=2\n"}),
        evaluator=QueueEvaluator(values=[13.876, 13.1, 13.05]),
        github=github,  # type: ignore[arg-type]
        bot_auth=NoAuth(),  # type: ignore[arg-type]
        now=1_000_000.0,
        created="2026-08-15T00:00:00Z",
        panel_lenses=(PanelLens("review", judge),),
    )
    assert outcome.outcome == "improved"
    pr = github.prs[0]
    assert pr["draft"] is True
    assert pr["body"].startswith("> **Draft")
    assert "suspicious lever" in pr["body"]
    assert github.armed == []  # a draft with open blocking findings never arms


def test_moved_base_gets_a_fresh_panel_read_and_blocking_drafts(tmp_path, target_repo) -> None:
    """The panel's verdict must hold on the tree that actually lands: clean at
    session time, blocking on the merged tree -> DRAFT PR, never armed."""
    import json as _json

    from autoresearch.panel import PanelLens

    clean = _json.dumps({"findings": [], "notes": "clean"})
    blocking = _json.dumps(
        {
            "findings": [
                {
                    "file": "src/pilot/solvers/tsp.py",
                    "line": 1,
                    "confidence": "high",
                    "summary": "merged-tree interaction looks gamed",
                    "detail": "d",
                    "blocking": True,
                }
            ],
            "notes": "",
        }
    )
    judge = _panel_judge([clean, blocking])
    github = FakeGitHub()
    outcome = live_climb(
        config=ClimbConfig(target="org/pilot", benchmark="tsp"),
        run_root=tmp_path / "state",
        run_id="tsp-panel-race",
        harness=RacingHarness(
            edits={"src/pilot/solvers/tsp.py": "r=9\n"},
            target_repo=target_repo,
            tmp_path=tmp_path,
        ),
        # session pair, then the post-merge climbed pair
        evaluator=QueueEvaluator(values=[13.876, 13.1, 13.9, 13.2]),
        github=github,  # type: ignore[arg-type]
        bot_auth=NoAuth(),  # type: ignore[arg-type]
        now=1_000_000.0,
        created="2026-08-15T00:00:00Z",
        panel_lenses=(PanelLens("review", judge),),
    )
    assert outcome.outcome == "improved"
    pr = github.prs[0]
    assert pr["draft"] is True
    assert "merged-tree interaction looks gamed" in pr["body"]
    assert "Verification round 2" in pr["body"]  # numbering continued post-merge
    assert github.armed == []
