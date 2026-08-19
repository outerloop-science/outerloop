"""LocalMeasurer: the inline Measurer backend. `clean` worktrees (content ==
sha) are cached; the `live` workspace is measured fresh every call."""

from __future__ import annotations

from pathlib import Path

import pytest

from autoresearch.measure import LocalMeasurer, Measure, SiblingSpec, plan_measures
from autoresearch.orchestrator import EvalError

BASE = "a" * 40
CAND = "b" * 40


class FakeEvaluator:
    """Returns a value keyed by (workspace, command); records every call."""

    def __init__(self, values: dict[tuple[Path, str], float]):
        self.values = values
        self.calls: list[tuple[Path, str, str, dict | None]] = []

    def evaluate(self, workspace, command, metric, extra_env=None):
        self.calls.append((workspace, command, metric, extra_env))
        return self.values[(workspace, command)]


def _trees(tmp_path: Path) -> tuple[Path, Path]:
    base, live = tmp_path / "base", tmp_path / "live"
    base.mkdir()
    live.mkdir()
    return base, live


def test_baseline_runs_clean_tree_candidate_runs_live(tmp_path):
    base, live = _trees(tmp_path)
    ev = FakeEvaluator({(base, "cmd"): 0.50, (live, "cmd"): 0.61})
    m = LocalMeasurer(ev, clean={BASE: base}, live=live)
    out = m.results(plan_measures("cmd", "r2", BASE, CAND))
    assert out == {"baseline": 0.50, "candidate": 0.61}
    assert ev.calls[0][:3] == (base, "cmd", "r2")  # baseline in the clean tree
    assert ev.calls[1][:3] == (live, "cmd", "r2")  # candidate in the live tree


def test_clean_tree_is_cached_live_tree_is_not(tmp_path):
    base, live = _trees(tmp_path)
    ev = FakeEvaluator({(base, "cmd"): 0.50, (live, "cmd"): 0.61})
    m = LocalMeasurer(ev, clean={BASE: base}, live=live)
    plan = plan_measures("cmd", "r2", BASE, CAND)
    m.results(plan)
    m.results(plan)
    # baseline (clean) measured once and cached; candidate (live) measured EVERY
    # call — a revision that changed only untracked files must not read stale.
    base_calls = [c for c in ev.calls if c[0] == base]
    live_calls = [c for c in ev.calls if c[0] == live]
    assert len(base_calls) == 1
    assert len(live_calls) == 2


def test_clean_cache_key_is_the_full_identity(tmp_path):
    base, _ = _trees(tmp_path)
    ev = FakeEvaluator({(base, "cmd1"): 0.1, (base, "cmd2"): 0.2})
    m = LocalMeasurer(ev, clean={BASE: base})
    # same name + tree, different command / env -> distinct clean-cache entries
    assert m.results([Measure("x", BASE, "cmd1", "r2")])["x"] == 0.1
    assert m.results([Measure("x", BASE, "cmd2", "r2")])["x"] == 0.2
    m.results([Measure("x", BASE, "cmd1", "r2", extra_env=(("S", "1"),))])
    m.results([Measure("x", BASE, "cmd1", "r2", extra_env=(("S", "2"),))])
    assert len(ev.calls) == 4  # 4 distinct identities, none collided


def test_unknown_sha_without_a_live_tree_is_eval_error(tmp_path):
    m = LocalMeasurer(FakeEvaluator({}), clean={})  # no clean tree, no live
    with pytest.raises(EvalError, match="no worktree"):
        m.results([Measure("baseline", BASE, "cmd", "r2")])


def test_eval_failure_names_the_measure(tmp_path):
    base, _ = _trees(tmp_path)

    class Boom:
        def evaluate(self, *a, **k):
            raise EvalError("harness crashed")

    m = LocalMeasurer(Boom(), clean={BASE: base})
    with pytest.raises(EvalError, match="baseline: harness crashed"):
        m.results([Measure("baseline", BASE, "cmd", "r2")])


def test_passes_the_paired_seed_to_both_sides(tmp_path):
    base, live = _trees(tmp_path)
    ev = FakeEvaluator({(base, "cmd"): 0.50, (live, "cmd"): 0.61})
    m = LocalMeasurer(ev, clean={BASE: base}, live=live)
    m.results(plan_measures("cmd", "r2", BASE, CAND, seed_env="S", seed=7))
    assert [c[3] for c in ev.calls] == [{"S": "7"}, {"S": "7"}]  # common random numbers


def test_no_seed_passes_none_not_empty_dict(tmp_path):
    base, live = _trees(tmp_path)
    ev = FakeEvaluator({(base, "cmd"): 0.50, (live, "cmd"): 0.61})
    m = LocalMeasurer(ev, clean={BASE: base}, live=live)
    m.results(plan_measures("cmd", "r2", BASE, CAND))
    assert all(c[3] is None for c in ev.calls)  # matches the evaluator's default


def test_siblings_run_base_clean_and_cand_live(tmp_path):
    base, live = _trees(tmp_path)
    ev = FakeEvaluator(
        {(base, "cmd"): 0.50, (live, "cmd"): 0.61, (base, "sibcmd"): 0.8, (live, "sibcmd"): 0.79}
    )
    m = LocalMeasurer(ev, clean={BASE: base}, live=live)
    plan = plan_measures(
        "cmd",
        "r2",
        BASE,
        CAND,
        siblings=(SiblingSpec("tsp", "sibcmd", "len", seed_env="T", seed=9),),
    )
    out = m.results(plan)
    assert out == {
        "baseline": 0.50,
        "candidate": 0.61,
        "sib-tsp-base": 0.8,
        "sib-tsp-cand": 0.79,
    }
    # base side of the sibling ran in the clean tree, cand side in the live tree
    sib = [c for c in ev.calls if c[1] == "sibcmd"]
    assert {c[0] for c in sib} == {base, live}
    assert all(c[3] == {"T": "9"} for c in sib)
