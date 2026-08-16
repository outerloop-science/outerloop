"""The requested lane: issues become runs, gated and claimed."""

from __future__ import annotations

from autoresearch.contract import load_contract
from autoresearch.intake import (
    CLAIM_MARKER,
    infer_benchmark,
    issue_hypothesis,
    pick_issue,
    qualifying_issue,
)

CONTRACT = load_contract(
    """
benchmarks:
  - name: tsp
    command: c1
    metric: m1
    direction: min
  - name: reach
    command: c2
    metric: m2
    direction: max
budgets: {gpu_hours_per_run: 1, runs_per_week: 10}
scope: {allowed: [src/]}
roadmap: docs/roadmap.md
""",
    "org/pilot",
)


def issue(n, title, body="", assoc="MEMBER", author="renmengye"):
    return {
        "number": n,
        "title": title,
        "body": body,
        "author_association": assoc,
        "user": {"login": author},
    }


class G:
    def __init__(self, issues, claimed=()):
        self.issues = issues
        self.claimed = set(claimed)

    def list_open_issues(self, repo, max_pages=3):
        return self.issues

    def list_comments(self, repo, number, max_pages=20):
        # claim markers count only when the BOT posted them (forgery guard)
        if number in self.claimed:
            return [{"body": f"{CLAIM_MARKER}\nPicked up", "user": {"login": "bot"}}]
        return []


def test_gate_and_benchmark_inference() -> None:
    assert qualifying_issue(issue(1, "improve reach"), "bot")
    assert not qualifying_issue(issue(2, "drive-by", assoc="NONE"), "bot")
    assert not qualifying_issue(issue(3, "self", author="bot"), "bot")
    assert infer_benchmark("the reach solver stalls", CONTRACT) == "reach"
    assert infer_benchmark("tsp and reach both bad", CONTRACT) == ""  # ambiguous
    assert infer_benchmark("nothing named", CONTRACT) == ""


def test_pick_oldest_unclaimed_with_one_benchmark() -> None:
    issues = [
        issue(5, "reach is weak"),
        issue(3, "improve tsp", assoc="NONE"),  # unqualified
        issue(4, "fix reach please"),  # oldest qualified... but claimed
    ]
    task = pick_issue(G(issues, claimed={4}), "org/pilot", CONTRACT, "bot")
    assert task is not None
    assert task.number == 5
    assert task.benchmark == "reach"


def test_released_claim_is_pickable_again_and_forgeries_ignored() -> None:
    from autoresearch.intake import RELEASE_MARKER

    class G2(G):
        def __init__(self, issues, comments):
            super().__init__(issues)
            self.comments = comments

        def list_comments(self, repo, number, max_pages=20):
            return self.comments

    released = [
        {"body": f"{CLAIM_MARKER}\nPicked up", "user": {"login": "bot"}},
        {"body": f"{RELEASE_MARKER}\nSubmission failed", "user": {"login": "bot"}},
    ]
    task = pick_issue(G2([issue(4, "fix reach please")], released), "org/pilot", CONTRACT, "bot")
    assert task is not None and task.number == 4  # released -> claimable again
    forged_release = [
        {"body": f"{CLAIM_MARKER}\nPicked up", "user": {"login": "bot"}},
        {"body": f"{RELEASE_MARKER}\nfree it!", "user": {"login": "mallory"}},
    ]
    assert (
        pick_issue(G2([issue(4, "fix reach please")], forged_release), "org/pilot", CONTRACT, "bot")
        is None
    )


def test_intake_attempt_cap_stops_the_claim_release_loop() -> None:
    from autoresearch.intake import MAX_INTAKE_ATTEMPTS, RELEASE_MARKER

    class G2(G):
        def __init__(self, issues, comments):
            super().__init__(issues)
            self.comments = comments

        def list_comments(self, repo, number, max_pages=20):
            return self.comments

    burned = []
    for _ in range(MAX_INTAKE_ATTEMPTS):
        burned.append({"body": f"{CLAIM_MARKER}\nPicked up", "user": {"login": "bot"}})
        burned.append({"body": f"{RELEASE_MARKER}\nSubmission failed", "user": {"login": "bot"}})
    assert (
        pick_issue(G2([issue(4, "fix reach please")], burned), "org/pilot", CONTRACT, "bot") is None
    )


def test_hypothesis_fences_issue_text() -> None:
    task = pick_issue(
        G([issue(7, "reach: try a better local planner", "the PD controller ```breaks```")]),
        "org/pilot",
        CONTRACT,
        "bot",
    )
    assert task is not None
    text = issue_hypothesis(task)
    assert "#7" in text
    assert "@renmengye" in text
    assert "````" in text  # fence outruns the backticks in the body
    assert "better local planner" in text


def test_intake_never_claims_steward_work_orders() -> None:
    """A steward-labeled issue naming one benchmark would otherwise qualify
    for the requested lane; the label reserves it for the steward."""
    from autoresearch.contract import load_contract
    from autoresearch.intake import pick_issue

    contract = load_contract(
        """
benchmarks:
  - {name: tsp, command: c, metric: m, direction: min}
budgets: {gpu_hours_per_run: 0, runs_per_week: 20}
scope: {allowed: [src/]}
roadmap: docs/roadmap.md
""",
        "org/pilot",
    )

    class G:
        def list_open_issues(self, repo, max_pages: int = 3):
            return [
                {
                    "number": 5,
                    "title": "tsp: make the pool resample",
                    "body": "",
                    "user": {"login": "renmengye"},
                    "author_association": "OWNER",
                    "labels": [{"name": "autoresearch:steward"}],
                }
            ]

        def list_comments(self, repo, number, max_pages: int = 20):
            return []

    assert pick_issue(G(), "org/pilot", contract, "agentic-learning-bot") is None
