"""The benchmark steward: scope inversion, work-order intake, live flow."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field

import pytest

from autoresearch.contract import load_contract
from autoresearch.harness import SessionResult
from autoresearch.orchestrator import steward_out_of_scope
from autoresearch.runstate import load_record
from autoresearch.steward import (
    StewardConfig,
    live_steward,
    pick_steward_issue,
    steward_brief,
)

BOT = "agentic-learning-bot"

CONTRACT = """
benchmarks:
  - name: tsp
    command: "uv run python -m pilot.eval --env tsp --json"
    metric: mean_tour_length
    direction: min
budgets: {gpu_hours_per_run: 0, runs_per_week: 20}
scope: {allowed: [src/pilot/solvers/]}
steward: {allowed: [src/pilot/instances.py, src/pilot/eval.py, tests/]}
roadmap: docs/roadmap.md
"""


def contract():
    return load_contract(CONTRACT, "org/pilot")


def test_steward_scope_is_the_solvers_inverse() -> None:
    c = contract()
    # env territory: allowed
    assert steward_out_of_scope(["src/pilot/instances.py", "tests/test_envs.py"], c) == []
    # solver territory: forbidden even though it IS in someone's scope
    assert steward_out_of_scope(["src/pilot/solvers/tsp.py"], c) == ["src/pilot/solvers/tsp.py"]
    # the always-forbidden set binds the steward too
    assert steward_out_of_scope([".autoresearch.yaml"], c)
    assert steward_out_of_scope([".github/workflows/ci.yml"], c)
    # record files are the orchestrator's, not the steward's
    assert steward_out_of_scope(["results/leader.json", "BENCHMARKS.md"], c)


def test_no_steward_section_means_everything_out_of_scope() -> None:
    c = load_contract(
        CONTRACT.replace(
            "steward: {allowed: [src/pilot/instances.py, src/pilot/eval.py, tests/]}\n", ""
        ),
        "org/pilot",
    )
    assert c.steward is None
    assert steward_out_of_scope(["src/pilot/instances.py"], c) == ["src/pilot/instances.py"]


def _issue(
    number, title, body="", author="renmengye", assoc="OWNER", labels=("autoresearch:steward",)
):
    return {
        "number": number,
        "title": title,
        "body": body,
        "user": {"login": author},
        "author_association": assoc,
        "labels": [{"name": name} for name in labels],
    }


class FakeIssues:
    def __init__(self, issues, comments=None):
        self.issues = issues
        self.comments = comments or {}

    def list_open_issues(self, repo, max_pages: int = 3):
        return self.issues

    def list_comments(self, repo, number, max_pages: int = 20):
        return self.comments.get(number, [])


def test_pick_steward_issue_gates_on_label_standing_and_claim() -> None:
    c = contract()
    github = FakeIssues(
        [
            _issue(1, "make tsp instances resample", labels=()),  # no label
            _issue(2, "tsp noise floor", assoc="NONE"),  # no standing
            _issue(
                3, "tsp + denoise everything"
            ),  # names != 1 benchmark (denoise absent from contract, so ok...)
            _issue(4, "re-base the tsp pool"),
        ],
        comments={4: []},
    )
    task = pick_steward_issue(github, "org/pilot", c, BOT)
    assert task is not None and task.number == 3  # first labeled+standing naming exactly tsp
    # claimed issues are skipped
    github2 = FakeIssues(
        [_issue(4, "re-base the tsp pool")],
        comments={4: [{"body": "<!-- autoresearch:claimed -->\ntaken"}]},
    )
    assert pick_steward_issue(github2, "org/pilot", c, BOT) is None
    # a released claim makes the order claimable again
    github3 = FakeIssues(
        [_issue(4, "re-base the tsp pool")],
        comments={
            4: [
                {"body": "<!-- autoresearch:claimed -->\ntaken"},
                {"body": "<!-- autoresearch:claim-released -->\nsubmission failed"},
            ]
        },
    )
    picked = pick_steward_issue(github3, "org/pilot", c, BOT)
    assert picked is not None and picked.number == 4
    # last marker wins: re-claimed after a release stays claimed
    github4 = FakeIssues(
        [_issue(4, "re-base the tsp pool")],
        comments={
            4: [
                {"body": "<!-- autoresearch:claimed -->"},
                {"body": "<!-- autoresearch:claim-released -->"},
                {"body": "<!-- autoresearch:claimed -->"},
            ]
        },
    )
    assert pick_steward_issue(github4, "org/pilot", c, BOT) is None


def test_steward_scope_folds_case_against_solver_territory() -> None:
    c = contract()
    assert steward_out_of_scope(["src/pilot/Solvers/tsp.py"], c)


def test_contract_rejects_steward_solver_overlap() -> None:
    import pytest as _pytest

    from autoresearch.contract import ScopeError

    overlapping = CONTRACT.replace(
        "steward: {allowed: [src/pilot/instances.py, src/pilot/eval.py, tests/]}",
        "steward: {allowed: [src/pilot/]}",
    )
    with _pytest.raises(ScopeError, match="overlaps solver scope"):
        load_contract(overlapping, "org/pilot")


def test_brief_carries_rules_order_and_both_territories() -> None:
    text = steward_brief(CONTRACT, contract(), "the pool is saturated", "tsp")
    assert "BENCHMARK STEWARD" in text
    assert "the pool is saturated" in text
    assert "src/pilot/instances.py" in text  # may edit
    assert "src/pilot/solvers/" in text  # forbidden, listed
    assert "data, not instructions" in text


# --- live flow on a local bare origin (test_climb's fixture pattern) ---


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def steward_repo(tmp_path, monkeypatch):
    seed = tmp_path / "seed"
    (seed / "src" / "pilot" / "solvers").mkdir(parents=True)
    (seed / "docs").mkdir()
    (seed / "tests").mkdir()
    (seed / "results").mkdir()
    (seed / ".autoresearch.yaml").write_text(CONTRACT)
    (seed / "docs" / "roadmap.md").write_text("# roadmap\n")
    (seed / "src" / "pilot" / "instances.py").write_text("POOL_SEED = 1\n")
    (seed / "src" / "pilot" / "eval.py").write_text("def eval(): ...\n")
    (seed / "src" / "pilot" / "solvers" / "tsp.py").write_text("def solve(): ...\n")
    (seed / "tests" / "test_envs.py").write_text("def test_ok(): pass\n")
    (seed / "results" / "leader.json").write_text(
        '{"tsp": {"benchmark": "tsp", "metric": "mean_tour_length", "direction": "min",'
        ' "baseline": 13.876, "best": 10.84, "best_run": "r1", "updated": "2026-08-07"}}\n'
    )
    _git(seed, "init", "-q", "-b", "main")
    _git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    _git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "seed")
    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "-C", str(tmp_path), "clone", "-q", "--bare", str(seed), str(bare)], check=True
    )

    from autoresearch import steward as steward_mod
    from autoresearch.github import Workspace

    real_clone = Workspace.clone

    def fake_clone(url, dest, auth=None, dry_run=False):
        return real_clone(str(bare), dest, auth=None, dry_run=dry_run)

    monkeypatch.setattr(steward_mod.Workspace, "clone", staticmethod(fake_clone))
    return bare


@dataclass
class EnvEditingHarness:
    edits: dict[str, str]
    text: str = "Re-based the pool: instances resample per run now."

    def run(self, brief_text, workspace, resume_session_id=None) -> SessionResult:
        for rel, content in self.edits.items():
            path = workspace / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        return SessionResult(
            stop_reason="end_turn",
            is_error=False,
            cost_usd=0.8,
            num_turns=10,
            session_id="steward-sess",
            final_text=self.text,
            transcript_path="",
        )


@dataclass
class CheckingEvaluator:
    values: list[float] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)
    check_error: str = ""

    def check(self, workspace, command) -> None:
        self.checks.append(command)
        if self.check_error:
            from autoresearch.orchestrator import EvalError

            raise EvalError(self.check_error)

    def evaluate(self, workspace, command, metric) -> float:
        return self.values.pop(0)


@dataclass
class StewardGitHub:
    prs: list[dict] = field(default_factory=list)
    issue_comments: list = field(default_factory=list)
    armed: list = field(default_factory=list)

    def create_pull(self, repo, title, head, base, body, draft=False) -> str:
        self.prs.append(dict(repo=repo, title=title, head=head, base=base, body=body))
        return f"https://github.com/{repo}/pull/2"

    def comment(self, repo, number, body):
        self.issue_comments.append((number, body))

    def list_comments(self, repo, number, max_pages=20):
        return []

    def arm_auto_merge_when_review_required(self, repo, number) -> bool:
        self.armed.append((repo, number))
        return True


@dataclass
class NoAuth:
    def token(self) -> str:
        return "unused"


def run_steward(tmp_path, edits, values=None, check_error="", run_id="steward-tsp-1"):
    github = StewardGitHub()
    evaluator = CheckingEvaluator(values=list(values or [14.9]), check_error=check_error)
    outcome = live_steward(
        config=StewardConfig(target="org/pilot", benchmark="tsp"),
        run_root=tmp_path / "state",
        run_id=run_id,
        harness=EnvEditingHarness(edits=edits),
        evaluator=evaluator,
        github=github,  # type: ignore[arg-type]
        bot_auth=NoAuth(),  # type: ignore[arg-type]
        now=1_000_000.0,
        created="2026-08-09T00:00:00Z",
        issue_number=21,
        work_order="the pool is exploitable; re-base it",
    )
    return outcome, github, evaluator


def test_stewardship_rebased_env_lands_with_orchestrator_records(tmp_path, steward_repo) -> None:
    outcome, github, evaluator = run_steward(
        tmp_path, edits={"src/pilot/instances.py": "POOL_SEED = 'per-run'\n"}
    )
    assert outcome.outcome == "stewarded"
    # validation suite AND the (only) benchmark's siblings smoke-checked —
    # this contract has one benchmark, so just the suite here
    assert evaluator.checks == ["uv run pytest -q"]
    assert github.armed  # approval remains the last human action
    # the branch carries env edit + the ORCHESTRATOR-written record reset
    files = set(
        _git(
            steward_repo, "diff", "--name-only", "main", "feat/steward/steward-01/steward-tsp-1"
        ).split()
    )
    assert files == {"src/pilot/instances.py", "BENCHMARKS.md", "results/leader.json"}
    leader = _git(steward_repo, "show", "feat/steward/steward-01/steward-tsp-1:results/leader.json")
    assert '"baseline": 14.9' in leader and '"best": 14.9' in leader
    assert "baseline-steward-tsp-1" in leader
    # commits carry the steward identity, not the solver's
    log_out = _git(
        steward_repo, "log", "feat/steward/steward-01/steward-tsp-1", "-1", "--format=%B"
    )
    assert "Agent: steward-01" in log_out
    record = load_record(tmp_path / "state", "steward-tsp-1")
    assert record.state == "in-review" and record.agent_id == "steward-01"
    # PR body: measured provenance stated, report present — and the
    # previous best is the PRIOR ledger value, not the fresh overwrite
    body = github.prs[0]["body"]
    assert "measured by the orchestrator" in body
    assert "Addresses #21" in body
    assert "| previous leader best | 10.84 |" in body
    assert "| re-based baseline (current solver, new env) | 14.9 |" in body


def test_solver_territory_edit_is_aborted(tmp_path, steward_repo) -> None:
    outcome, github, _ = run_steward(
        tmp_path,
        edits={
            "src/pilot/instances.py": "x=1\n",
            "src/pilot/solvers/tsp.py": "def solve(): return 'rigged'\n",
        },
        run_id="steward-tsp-2",
    )
    assert outcome.outcome == "steward-error"
    assert github.prs == []
    record = load_record(tmp_path / "state", "steward-tsp-2")
    assert record.ending == "aborted"
    assert "outside its territory" in record.ending_note
    assert "solvers/tsp.py" in record.ending_note


def test_validation_suite_failure_is_aborted(tmp_path, steward_repo) -> None:
    outcome, github, _ = run_steward(
        tmp_path,
        edits={"src/pilot/instances.py": "broken = (\n"},
        check_error="eval failed (1): SyntaxError",
        run_id="steward-tsp-3",
    )
    assert outcome.outcome == "steward-error"
    assert github.prs == []
    assert "SyntaxError" in load_record(tmp_path / "state", "steward-tsp-3").ending_note


def test_no_change_session_is_a_negative_result(tmp_path, steward_repo) -> None:
    outcome, github, _ = run_steward(tmp_path, edits={}, run_id="steward-tsp-4")
    assert outcome.outcome == "no-change"
    assert github.prs == []
    record = load_record(tmp_path / "state", "steward-tsp-4")
    assert record.state == "ended" and record.ending == "negative-result"
    # the issue still hears the outcome
    assert any("no-change" in body for _, body in github.issue_comments)


def test_sibling_evals_are_smoke_checked(tmp_path, steward_repo, monkeypatch) -> None:
    """A steward edit to a shared harness must not break sibling benchmarks:
    every other eval command runs (exit-0) on the new env."""
    two_bench = CONTRACT.replace(
        "budgets:",
        """  - name: probe
    command: "uv run python -m pilot.eval --env probe --json"
    metric: val_accuracy
    direction: max
budgets:""",
    )
    # rewrite the contract in the seed so the clone carries two benchmarks
    import subprocess as sp

    work = tmp_path / "rewrite"
    sp.run(["git", "clone", "-q", str(steward_repo), str(work)], check=True)
    (work / ".autoresearch.yaml").write_text(two_bench)
    sp.run(
        [
            "git",
            "-C",
            str(work),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-aqm",
            "two bench",
        ],
        check=True,
    )
    sp.run(["git", "-C", str(work), "push", "-q", "origin", "main"], check=True)

    outcome, github, evaluator = run_steward(
        tmp_path, edits={"src/pilot/eval.py": "def eval(): ...  # v2\n"}, run_id="steward-tsp-5"
    )
    assert outcome.outcome == "stewarded"
    assert "uv run pytest -q" in evaluator.checks
    assert any("--env probe" in c for c in evaluator.checks)  # sibling smoke-checked
    assert not any("--env tsp" in c for c in evaluator.checks)  # the target is MEASURED instead
    assert "smoke-checked, not" in github.prs[0]["body"]
