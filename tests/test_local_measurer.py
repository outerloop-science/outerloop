"""LocalMeasurer: the inline Measurer backend. It measures the caller's
existing worktrees (no checkout), caches per identity, and never parks."""

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
    base, cand = tmp_path / "base", tmp_path / "cand"
    base.mkdir()
    cand.mkdir()
    return base, cand


def test_measures_each_tree_with_the_climb_command(tmp_path):
    base, cand = _trees(tmp_path)
    ev = FakeEvaluator({(base, "cmd"): 0.50, (cand, "cmd"): 0.61})
    m = LocalMeasurer(ev, {BASE: base, CAND: cand})
    out = m.results(plan_measures("cmd", "r2", BASE, CAND))
    assert out == {"baseline": 0.50, "candidate": 0.61}
    # baseline ran in the base tree, candidate in the candidate tree
    assert (base, "cmd", "r2") == ev.calls[0][:3]
    assert (cand, "cmd", "r2") == ev.calls[1][:3]


def test_caches_repeated_identity(tmp_path):
    base, cand = _trees(tmp_path)
    ev = FakeEvaluator({(base, "cmd"): 0.50, (cand, "cmd"): 0.61})
    m = LocalMeasurer(ev, {BASE: base, CAND: cand})
    plan = plan_measures("cmd", "r2", BASE, CAND)
    m.results(plan)
    m.results(plan)  # same identities -> served from cache
    assert len(ev.calls) == 2  # each measured once, not four times


def test_cache_key_is_the_full_identity_not_just_name_and_tree(tmp_path):
    # a weaker key (name+tree only) would collide these into one stale value.
    base, _ = _trees(tmp_path)
    ev = FakeEvaluator({(base, "cmd1"): 0.1, (base, "cmd2"): 0.2})
    m = LocalMeasurer(ev, {BASE: base})
    # same name + tree, DIFFERENT command -> distinct results, not a cache hit
    assert m.results([Measure("x", BASE, "cmd1", "r2")])["x"] == 0.1
    assert m.results([Measure("x", BASE, "cmd2", "r2")])["x"] == 0.2
    # same name + tree + command, DIFFERENT env (seed) -> a fresh eval each
    m.results([Measure("x", BASE, "cmd1", "r2", extra_env=(("S", "1"),))])
    m.results([Measure("x", BASE, "cmd1", "r2", extra_env=(("S", "2"),))])
    assert len(ev.calls) == 4  # 4 distinct identities -> 4 evals, none collided


def test_missing_worktree_is_eval_error(tmp_path):
    m = LocalMeasurer(FakeEvaluator({}), {})  # no trees registered
    with pytest.raises(EvalError, match="no worktree"):
        m.results([Measure("baseline", BASE, "cmd", "r2")])


def test_passes_the_paired_seed_to_both_sides(tmp_path):
    base, cand = _trees(tmp_path)
    ev = FakeEvaluator({(base, "cmd"): 0.50, (cand, "cmd"): 0.61})
    m = LocalMeasurer(ev, {BASE: base, CAND: cand})
    m.results(plan_measures("cmd", "r2", BASE, CAND, seed_env="S", seed=7))
    assert [c[3] for c in ev.calls] == [{"S": "7"}, {"S": "7"}]  # common random numbers


def test_no_seed_passes_none_not_empty_dict(tmp_path):
    base, cand = _trees(tmp_path)
    ev = FakeEvaluator({(base, "cmd"): 0.50, (cand, "cmd"): 0.61})
    m = LocalMeasurer(ev, {BASE: base, CAND: cand})
    m.results(plan_measures("cmd", "r2", BASE, CAND))
    assert all(c[3] is None for c in ev.calls)  # matches the evaluator's default


def test_siblings_run_their_own_command_on_the_right_trees(tmp_path):
    base, cand = _trees(tmp_path)
    ev = FakeEvaluator(
        {(base, "cmd"): 0.50, (cand, "cmd"): 0.61, (base, "sibcmd"): 0.8, (cand, "sibcmd"): 0.79}
    )
    m = LocalMeasurer(ev, {BASE: base, CAND: cand})
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
    # the sibling pair ran sibcmd on base and cand, each carrying the suite seed
    sib_calls = [c for c in ev.calls if c[1] == "sibcmd"]
    assert {c[0] for c in sib_calls} == {base, cand}
    assert all(c[3] == {"T": "9"} for c in sib_calls)
