"""The benchmark steward: scope inversion, work-order intake, live flow."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field

import pytest

from autoresearch.contract import load_contract
from autoresearch.harness import SessionResult
from autoresearch.intake import CLAIM_MARKER
from autoresearch.orchestrator import steward_out_of_scope
from autoresearch.runstate import load_record, outage_active
from autoresearch.steward import (
    OUTAGE_MARKER,
    RELEASE_MARKER,
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
        comments={4: [{"body": "<!-- autoresearch:claimed -->\ntaken", "user": {"login": BOT}}]},
    )
    assert pick_steward_issue(github2, "org/pilot", c, BOT) is None
    # a released claim makes the order claimable again
    github3 = FakeIssues(
        [_issue(4, "re-base the tsp pool")],
        comments={
            4: [
                {"body": "<!-- autoresearch:claimed -->\ntaken", "user": {"login": BOT}},
                {
                    "body": "<!-- autoresearch:claim-released -->\nsubmission failed",
                    "user": {"login": BOT},
                },
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
                {"body": "<!-- autoresearch:claimed -->", "user": {"login": BOT}},
                {"body": "<!-- autoresearch:claim-released -->", "user": {"login": BOT}},
                {"body": "<!-- autoresearch:claimed -->", "user": {"login": BOT}},
            ]
        },
    )
    assert pick_steward_issue(github4, "org/pilot", c, BOT) is None
    # attempt cap: three total claims -> a human's turn, even if released
    github5 = FakeIssues(
        [_issue(4, "re-base the tsp pool")],
        comments={
            4: [
                {"body": "<!-- autoresearch:claimed -->", "user": {"login": BOT}},
                {"body": "<!-- autoresearch:claim-released -->", "user": {"login": BOT}},
                {"body": "<!-- autoresearch:claimed -->", "user": {"login": BOT}},
                {"body": "<!-- autoresearch:claim-released -->", "user": {"login": BOT}},
                {"body": "<!-- autoresearch:claimed -->", "user": {"login": BOT}},
                {"body": "<!-- autoresearch:claim-released -->", "user": {"login": BOT}},
            ]
        },
    )
    assert pick_steward_issue(github5, "org/pilot", c, BOT) is None
    # and it is the ATTEMPT COUNT holding, not a lingering claim flag:
    # the same thread ends released, so only the cap can exclude it
    released_last = github5.comments[4][-1]
    assert "claim-released" in released_last["body"]


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
    # the three-tier mission is in the constitution, invention ending at a
    # proposal (the contract is not the steward's to write)
    assert "MAINTAIN" in text and "EXTEND" in text and "INVENT" in text
    assert "NOT yours" in text  # invention ends at a proposal
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

    def evaluate(self, workspace, command, metric, extra_env=None) -> float:
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
        bot_auth=NoAuth(),
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


def test_exhausted_session_is_a_budget_ending_not_an_error(tmp_path, steward_repo) -> None:
    """Turns running out is one of the six honest deaths: the record says
    budget-exhausted with the real cause, and the work order hears "ran
    out of its session budget" — not "ValueError: tool_use"."""

    @dataclass
    class DryHarness:
        def run(self, brief_text, workspace, resume_session_id=None) -> SessionResult:
            return SessionResult(
                stop_reason="tool_use",
                is_error=True,
                cost_usd=2.0,
                num_turns=120,
                session_id="steward-sess",
                final_text="",
                transcript_path="",
                error_detail="error_max_turns: Reached maximum number of turns (120)",
            )

    github = StewardGitHub()
    outcome = live_steward(
        config=StewardConfig(target="org/pilot", benchmark="tsp"),
        run_root=tmp_path / "state",
        run_id="steward-tsp-dry",
        harness=DryHarness(),
        evaluator=CheckingEvaluator(values=[14.9]),
        github=github,  # type: ignore[arg-type]
        bot_auth=NoAuth(),
        now=1_000_000.0,
        created="2026-08-09T00:00:00Z",
        issue_number=21,
        work_order="the pool is exploitable; re-base it",
    )
    assert outcome.outcome == "budget-exhausted"
    record = load_record(tmp_path / "state", "steward-tsp-dry")
    assert record.ending == "budget-exhausted"
    assert "maximum number of turns" in record.ending_note
    assert "ValueError" not in record.ending_note
    assert any(
        "claim-released" in body and "ran out of its session budget" in body
        for _, body in github.issue_comments
    )


def test_api_outage_releases_claim_without_counting(tmp_path, steward_repo) -> None:
    """The API refusing us is the orchestrator's failure: the run ends
    stuck, the release comment carries the outage marker (so the claim
    does not count toward the cap), and the latch pauses the lanes."""

    @dataclass
    class RefusedHarness:
        def run(self, brief_text, workspace, resume_session_id=None) -> SessionResult:
            return SessionResult(
                stop_reason="end_turn",
                is_error=True,
                cost_usd=0.0,
                num_turns=1,
                session_id="",
                final_text="",
                transcript_path="",
                error_detail="error_during_execution: credit balance is too low",
            )

    github = StewardGitHub()
    outcome = live_steward(
        config=StewardConfig(target="org/pilot", benchmark="tsp"),
        run_root=tmp_path / "state",
        run_id="steward-tsp-out",
        harness=RefusedHarness(),
        evaluator=CheckingEvaluator(values=[14.9]),
        github=github,  # type: ignore[arg-type]
        bot_auth=NoAuth(),
        now=1_000_000.0,
        created="2026-08-09T00:00:00Z",
        issue_number=21,
        work_order="w",
    )
    assert outcome.outcome == "infra-outage"
    record = load_record(tmp_path / "state", "steward-tsp-out")
    assert record.ending == "stuck"
    assert "credit balance" in record.ending_note
    (release,) = [b for _, b in github.issue_comments if RELEASE_MARKER in b]
    assert OUTAGE_MARKER in release and "does NOT count" in release
    assert "credit balance" in outage_active(
        tmp_path / "state", now=1_000_000.0 + 60, role="steward"
    )
    # and the STEWARD'S dead key never pauses the solver lanes
    assert outage_active(tmp_path / "state", now=1_000_000.0 + 60, role="solver") == ""


def test_outage_releases_do_not_count_toward_the_attempt_cap() -> None:
    """Three claims, one of them released by an outage: two attempts —
    the order is still claimable. Three COUNTED claims: capped."""

    def claim(author: str = BOT):
        return {"body": f"{CLAIM_MARKER}\nworking", "user": {"login": author}}

    def release(outage_kind: bool, author: str = BOT):
        marker = f"{RELEASE_MARKER}\n{OUTAGE_MARKER}" if outage_kind else RELEASE_MARKER
        return {"body": f"{marker}\nreleased", "user": {"login": author}}

    issue = {
        "number": 5,
        "title": "steward: harden tsp",
        "body": "the tsp pool is stale",
        "user": {"login": "renmengye", "type": "User"},
        "author_association": "OWNER",
        "labels": [{"name": "autoresearch:steward"}],
    }

    @dataclass
    class G:
        comments: list

        def list_open_issues(self, repo):
            return [issue]

        def list_comments(self, repo, number):
            return self.comments

    contract = load_contract(CONTRACT, "org/pilot")
    thread = [claim(), release(True), claim(), release(False), claim(), release(False)]
    picked = pick_steward_issue(G(thread), "org/pilot", contract, BOT)
    assert picked is not None and picked.number == 5  # 2 counted attempts
    thread += [claim(), release(False)]
    assert pick_steward_issue(G(thread), "org/pilot", contract, BOT) is None  # 3 counted


def test_marker_spoofing_by_strangers_moves_nothing() -> None:
    """Only the BOT'S comments move claim state (review finding, public-repo
    threat): a stranger's outage-release must not erase attempts into an
    unbounded paid retry loop, a stranger's release must not free a live
    claim, and a stranger's claim must not burn attempts."""

    def marked(body: str, author: str):
        return {"body": body, "user": {"login": author}}

    issue = {
        "number": 6,
        "title": "steward: harden tsp",
        "body": "the tsp pool is stale",
        "user": {"login": "renmengye", "type": "User"},
        "author_association": "OWNER",
        "labels": [{"name": "autoresearch:steward"}],
    }

    @dataclass
    class G:
        comments: list

        def list_open_issues(self, repo):
            return [issue]

        def list_comments(self, repo, number):
            return self.comments

    contract = load_contract(CONTRACT, "org/pilot")
    # capped order + stranger outage-releases: attempts stay at 3
    thread = [marked(f"{CLAIM_MARKER}\nx", BOT) for _ in range(3)]
    thread += [marked(f"{RELEASE_MARKER}\n{OUTAGE_MARKER}\nnice try", "stranger")] * 3
    assert pick_steward_issue(G(thread), "org/pilot", contract, BOT) is None
    # live claim + stranger release: still claimed
    thread2 = [marked(f"{CLAIM_MARKER}\nx", BOT), marked(f"{RELEASE_MARKER}\nfree it", "stranger")]
    assert pick_steward_issue(G(thread2), "org/pilot", contract, BOT) is None
    # stranger claims alone: not claimed, no attempts burned
    thread3 = [marked(f"{CLAIM_MARKER}\nmine now", "stranger")] * 5
    assert pick_steward_issue(G(thread3), "org/pilot", contract, BOT) is not None


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
    # the failure RELEASES the claim so the order is not orphaned
    assert any(
        "claim-released" in body and "steward-error" in body for _, body in github.issue_comments
    )


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


def test_contract_rejects_steward_scope_over_the_ledger() -> None:
    import pytest as _pytest

    from autoresearch.contract import ScopeError

    for bad in ("results/leader.json", "BENCHMARKS.md", "results/"):
        with _pytest.raises(ScopeError, match="record ledger"):
            load_contract(
                CONTRACT.replace(
                    "steward: {allowed: [src/pilot/instances.py, src/pilot/eval.py, tests/]}",
                    f"steward: {{allowed: [{bad}]}}",
                ),
                "org/pilot",
            )


def test_orphaned_claims_are_released_for_dead_runs() -> None:
    """Killed jobs never post their own release: reconciliation does."""
    from autoresearch.runstate import RunRecord
    from autoresearch.steward import release_orphaned_claims

    class G(FakeIssues):
        def __init__(self, issues, comments):
            super().__init__(issues, comments)
            self.posted = []

        def comment(self, repo, number, body):
            self.posted.append((number, body))

    claimed = {
        "body": "<!-- autoresearch:claimed -->",
        "created_at": "2026-08-09T00:00:00Z",
        "user": {"login": "agentic-learning-bot"},
    }
    dead_record = RunRecord(
        run_id="steward-tsp-9",
        target="org/pilot",
        task_title="t",
        state="ended",
        ending="aborted",
        agent_id="steward-01",
        issue_number=7,
    )
    github = G([_issue(7, "re-base the tsp pool")], {7: [claimed]})
    n = release_orphaned_claims(
        github, "org/pilot", [dead_record], now=2_000_000_000.0, bot_login="agentic-learning-bot"
    )
    assert n == 1
    assert github.posted and "claim-released" in github.posted[0][1]
    # a LIVE run keeps its claim
    live_record = RunRecord(
        run_id="steward-tsp-9",
        target="org/pilot",
        task_title="t",
        state="implementing",
        agent_id="steward-01",
        issue_number=7,
    )
    github2 = G([_issue(7, "re-base the tsp pool")], {7: [claimed]})
    assert (
        release_orphaned_claims(
            github2,
            "org/pilot",
            [live_record],
            now=2_000_000_000.0,
            bot_login="agentic-learning-bot",
        )
        == 0
    )
    # no record + stale claim -> released; fresh claim -> kept
    github3 = G([_issue(7, "re-base the tsp pool")], {7: [claimed]})
    assert (
        release_orphaned_claims(
            github3, "org/pilot", [], now=2_000_000_000.0, bot_login="agentic-learning-bot"
        )
        == 1
    )
    # now=2e9 is 2033-05-18T03:33Z; a claim 33 minutes old is not stale
    fresh = {
        "body": "<!-- autoresearch:claimed -->",
        "created_at": "2033-05-18T03:00:00Z",
        "user": {"login": "agentic-learning-bot"},
    }
    github4 = G([_issue(7, "re-base the tsp pool")], {7: [fresh]})
    assert (
        release_orphaned_claims(
            github4, "org/pilot", [], now=2_000_000_000.0, bot_login="agentic-learning-bot"
        )
        == 0
    )


def test_rebase_row_records_the_measurement_seed(tmp_path) -> None:
    """A re-based baseline on a resampled pool is re-derivable: the row
    carries the seed the orchestrator measured under."""
    import json as _json

    from autoresearch.steward import rebase_leader_row

    contract = load_contract(CONTRACT, "org/pilot")
    bench = contract.benchmarks[0]
    rebase_leader_row(
        tmp_path,
        contract,
        bench.name,
        bench,
        14.9,
        "steward-tsp-s",
        "2026-08-09",
        "org/pilot",
        run_seed=987654321,
    )
    raw = _json.loads((tmp_path / "results" / "leader.json").read_text())
    assert raw[bench.name]["run_seed"] == 987654321
    assert raw[bench.name]["best_run"] == "baseline-steward-tsp-s"


def test_read_only_spec_is_refused_before_any_work(tmp_path, steward_repo) -> None:
    # the steward edits env code; a non-executing spec here is a deployment
    # bug — loud and immediate, before the record or any network work
    from autoresearch.rolespec import Execution, RoleSpec, SessionBudget
    from autoresearch.steward import StewardConfig, live_steward

    read_only = RoleSpec(
        name="reviewer",
        instructions="x",
        key="reviewer",
        tools=("Read",),
        execution=Execution(environment="gh-runner", can_execute=False),
        budget=SessionBudget(max_turns=1, walltime_s=1),
    )

    with pytest.raises(ValueError, match="must allow execution"):
        live_steward(
            config=StewardConfig(target="org/pilot", benchmark="tsp"),
            run_root=tmp_path / "state",
            run_id="stw-badspec",
            harness=None,  # type: ignore[arg-type]  # refused before any use
            evaluator=None,  # type: ignore[arg-type]
            github=None,  # type: ignore[arg-type]
            bot_auth=None,  # type: ignore[arg-type]
            now=1_000_000.0,
            created="t",
            spec=read_only,
        )
    # "before any work" made checkable: no run dir, no record, nothing on disk
    assert not (tmp_path / "state").exists()
