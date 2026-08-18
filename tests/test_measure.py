"""The dispatched measurer: submit-park-resume with the cluster as the
authoritative liveness source (crash-safe against the submit/marker gap)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autoresearch.compute import CommandResult, SlurmCompute
from autoresearch.measure import (
    DispatchedMeasurer,
    Measure,
    MeasurementPending,
    SiblingSpec,
    plan_measures,
)
from autoresearch.orchestrator import EvalError


def _measurer(
    tmp_path: Path, submitted: list, live: dict | None = None, squeue_fails: bool = False
) -> DispatchedMeasurer:
    """`live` maps job-name -> id for jobs squeue should report as running."""
    live = live or {}

    def runner(argv, timeout_s):
        if argv and argv[0] == "sbatch":
            submitted.append(list(argv))
            return CommandResult(0, f"{100 + len(submitted)}\n", "")
        if argv and argv[0] == "squeue":
            if squeue_fails:
                return CommandResult(1, "", "squeue down")
            name = argv[argv.index("--name") + 1]
            return CommandResult(0, (live.get(name, "") + "\n") if live.get(name) else "", "")
        return CommandResult(0, "COMPLETED\n", "")

    return DispatchedMeasurer(
        compute=SlurmCompute(runner=runner),
        run_dir=tmp_path,
        repo_root=tmp_path / "repo",
        image="/img.sif",
        account="a",
        partition="p",
        eval_minutes=60,
        run_tag="r1",
    )


def _land(m: DispatchedMeasurer, measure: Measure, value=None, code="0", job="101"):
    # land a completed eval into the measure's own storage slot — derived from
    # the measurer so the test never hardcodes the identity formula.
    ev = m.run_dir / f"eval-{m._slot(measure)}"
    ev.mkdir(parents=True, exist_ok=True)
    (ev / "submitted").write_text(job)
    (ev / "exit-code").write_text(code)
    if value is not None:
        (ev / "stdout").write_text(json.dumps({"metric": "r2", "value": value}) + "\n")


def _dispatched(m: DispatchedMeasurer, measure: Measure, job="101"):
    ev = m.run_dir / f"eval-{m._slot(measure)}"
    ev.mkdir(parents=True, exist_ok=True)
    (ev / "submitted").write_text(job)


def _measures():
    return [Measure("baseline", "a" * 40, "cmd", "r2"), Measure("candidate", "b" * 40, "cmd", "r2")]


def test_first_pass_dispatches_all_and_parks(tmp_path):
    submitted: list = []
    m = _measurer(tmp_path, submitted)  # nothing live -> both dispatched
    with pytest.raises(MeasurementPending) as exc:
        m.results(_measures())
    assert len(submitted) == 2
    assert exc.value.afterany() == "afterany:101:102"


def test_resume_reads_completed_without_resubmitting(tmp_path):
    submitted: list = []
    m = _measurer(tmp_path, submitted)
    base, cand = _measures()
    _land(m, base, 0.50)
    _land(m, cand, 0.61)
    assert m.results(_measures()) == {"baseline": 0.50, "candidate": 0.61}
    assert submitted == []


def test_live_job_reparks_on_real_id_never_resubmits(tmp_path):
    """A job squeue reports as running is parked on its real id — even if its
    local marker is MISSING (the crash-before-marker case)."""
    submitted: list = []
    base, cand = _measures()
    cand_job = _measurer(tmp_path, [])._job_name(cand)
    m = _measurer(tmp_path, submitted, live={cand_job: "102"})
    _land(m, base, 0.50)
    # candidate has NO marker (submitter died in the gap) but IS live
    with pytest.raises(MeasurementPending) as exc:
        m.results(_measures())
    assert exc.value.afterany() == "afterany:102"
    assert submitted == []  # not resubmitted despite the missing marker


def test_dispatched_but_vanished_fails_not_resubmits(tmp_path):
    """A measure with a marker but no live job and no result died before
    producing a result (SIGKILL / GONE) -> EvalError, never resubmit."""
    submitted: list = []
    m = _measurer(tmp_path, submitted, live={})  # nothing live
    base, cand = _measures()
    _land(m, base, 0.50)
    _dispatched(m, cand, job="102")  # marker, not live, no result
    with pytest.raises(EvalError, match="vanished"):
        m.results(_measures())
    assert submitted == []


def test_nonzero_exit_raises(tmp_path):
    m = _measurer(tmp_path, [])
    base, cand = _measures()
    _land(m, base, 0.50)
    _land(m, cand, code="97")
    with pytest.raises(EvalError, match="candidate"):
        m.results(_measures())


def test_squeue_blind_parks_without_dispatch(tmp_path):
    """A transient squeue failure must not risk a duplicate: park (on the
    marker id if any), let the sweep deadline retry; never dispatch blind."""
    submitted: list = []
    m = _measurer(tmp_path, submitted, squeue_fails=True)
    base, _cand = _measures()
    _dispatched(m, base, job="102")  # has a marker
    # candidate has no marker; blind -> park, empty dep -> sweep handles it
    with pytest.raises(MeasurementPending) as exc:
        m.results(_measures())
    assert submitted == []
    assert exc.value.afterany() in ("afterany:102", "")  # marker id, never a resubmit


def test_measure_carries_paired_seed():
    m = Measure("sib", "c" * 40, "cmd", "r2", extra_env=(("PILOT_SEED", "7"),))
    assert m.env() == {"PILOT_SEED": "7"}


def test_empty_pending_afterany_is_blank():
    assert MeasurementPending(()).afterany() == ""


def test_plan_baseline_and_candidate_paired_seed():
    plan = plan_measures("cmd", "r2", "a" * 40, "b" * 40, seed_env="S", seed=7)
    assert [m.name for m in plan] == ["baseline", "candidate"]
    assert plan[0].tree_sha == "a" * 40 and plan[1].tree_sha == "b" * 40
    assert plan[0].env() == {"S": "7"} and plan[1].env() == {"S": "7"}  # common random numbers


def test_plan_no_seed_env_no_extra_env():
    plan = plan_measures("cmd", "r2", "a" * 40, "b" * 40)
    assert all(m.env() == {} for m in plan)


def test_plan_seed_zero_sentinel_is_not_injected():
    # 0 is the "no seed recorded" sentinel — a seeded benchmark whose seed is
    # unset (0) must inject NO seed var, not a literal "S=0" real seed.
    plan = plan_measures("cmd", "r2", "a" * 40, "b" * 40, seed_env="S", seed=0)
    assert all(m.env() == {} for m in plan)
    sib = SiblingSpec("tsp", "tspcmd", "len", seed_env="TSP_SEED", seed=0)
    assert sib.env() == ()


def test_plan_suite_siblings_use_their_own_seed_env():
    plan = plan_measures(
        "cmd",
        "r2",
        "a" * 40,
        "b" * 40,
        seed_env="HP_SEED",
        seed=7,
        siblings=(
            SiblingSpec("tsp", "tspcmd", "len", seed_env="TSP_SEED", seed=42),
            SiblingSpec("reach", "rcmd", "succ"),  # no seed
        ),
    )
    names = [m.name for m in plan]
    assert names == [
        "baseline",
        "candidate",
        "sib-tsp-base",
        "sib-tsp-cand",
        "sib-reach-base",
        "sib-reach-cand",
    ]
    # the climbed benchmark's seed does NOT leak onto siblings
    tsp_base = next(m for m in plan if m.name == "sib-tsp-base")
    tsp_cand = next(m for m in plan if m.name == "sib-tsp-cand")
    assert tsp_base.env() == {"TSP_SEED": "42"}  # its OWN var and seed
    assert tsp_cand.env() == {"TSP_SEED": "42"}  # paired on the sibling's seed
    assert "HP_SEED" not in tsp_base.env()
    assert tsp_base.command == "tspcmd" and tsp_base.metric == "len"
    # a sibling with no seed gets no env, regardless of the climbed seed
    assert next(m for m in plan if m.name == "sib-reach-base").env() == {}


def test_long_measure_names_get_distinct_job_names(tmp_path):
    """Two long measure names sharing a 60-char prefix must get distinct
    Slurm job names (a plain truncation would collide them into one job)."""
    from autoresearch.compute import CommandResult, SlurmCompute

    m = DispatchedMeasurer(
        compute=SlurmCompute(runner=lambda a, t: CommandResult(0, "", "")),
        run_dir=tmp_path,
        repo_root=tmp_path,
        image="/i",
        account="a",
        partition="p",
        eval_minutes=60,
        run_tag="r1",
    )
    a = Measure("sib-" + "x" * 60 + "-base", "a" * 40, "c", "r2")
    b = Measure("sib-" + "x" * 60 + "-cand", "a" * 40, "c", "r2")
    ja, jb = m._job_name(a), m._job_name(b)
    assert ja != jb and len(ja) <= 60 and len(jb) <= 60


def test_long_run_tags_get_distinct_job_names(tmp_path):
    """Two run tags sharing their first chars must get distinct job names for
    the same measure (the hash covers the full run_tag, not just a prefix)."""
    from autoresearch.compute import CommandResult, SlurmCompute

    def mk(tag):
        return DispatchedMeasurer(
            compute=SlurmCompute(runner=lambda a, t: CommandResult(0, "", "")),
            run_dir=tmp_path,
            repo_root=tmp_path,
            image="/i",
            account="a",
            partition="p",
            eval_minutes=60,
            run_tag=tag,
        )

    meas = Measure("baseline", "a" * 40, "c", "r2")
    n1 = mk("heldout_probe-20260818-aaa")._job_name(meas)
    n2 = mk("heldout_probe-20260818-bbb")._job_name(meas)
    assert n1 != n2 and len(n1) <= 60 and len(n2) <= 60


def test_same_name_new_sha_gets_fresh_storage_and_job(tmp_path):
    """A panel revision re-measures `candidate` at a NEW sha. Storage identity
    includes the tree_sha, so the new sha must NOT read the old sha's cached
    eval dir and must NOT reuse its cluster job name — otherwise a revision
    would silently inherit the pre-revision result."""
    m = _measurer(tmp_path, [])
    v1 = Measure("candidate", "b" * 40, "cmd", "r2")
    v2 = Measure("candidate", "c" * 40, "cmd", "r2")  # revised candidate
    assert m._ev(v1) != m._ev(v2)  # distinct on-disk slots
    assert m._job_name(v1) != m._job_name(v2)  # distinct cluster jobs
    # a landed v1 result is invisible to a v2 read (no stale inheritance)
    _land(m, v1, 0.42)
    with pytest.raises(MeasurementPending):
        m.results([v2])  # v2's slot has no result -> dispatched, not read


def test_shared_sha_prefix_does_not_alias_storage(tmp_path):
    """Two commits sharing an 8-char SHA prefix but differing later must NOT
    share an eval dir — a truncated slot would let the second read the first's
    cached result."""
    m = _measurer(tmp_path, [])
    p = "a" * 8
    a = Measure("candidate", p + "b" * 32, "cmd", "r2")
    b = Measure("candidate", p + "c" * 32, "cmd", "r2")
    assert m._ev(a) != m._ev(b)
    assert m._job_name(a) != m._job_name(b)


def test_changed_contract_for_same_name_and_sha_is_not_aliased(tmp_path):
    """The cache key is the full measurement determinant, not just (name, sha):
    the same role at the same commit but a DIFFERENT command / metric / seed
    (e.g. a resume that re-fetched a changed contract) must land in a fresh
    eval dir and job, never read the result computed under the old command."""
    m = _measurer(tmp_path, [])
    sha = "d" * 40
    base = Measure("candidate", sha, "cmd --v1", "r2")
    new_cmd = Measure("candidate", sha, "cmd --v2", "r2")
    new_metric = Measure("candidate", sha, "cmd --v1", "auroc")
    new_seed = Measure("candidate", sha, "cmd --v1", "r2", extra_env=(("S", "7"),))
    slots = {m._slot(x) for x in (base, new_cmd, new_metric, new_seed)}
    jobs = {m._job_name(x) for x in (base, new_cmd, new_metric, new_seed)}
    assert len(slots) == 4  # every determinant change gets its own storage
    assert len(jobs) == 4  # and its own cluster job
    # the durable slot's disambiguator is the FULL sha1 hex (no truncated
    # prefix that two distinct determinants could birthday-collide into)
    assert len(m._slot(base).split("-")[-1]) == 40
    # a v1 result must not satisfy a v2-command read
    _land(m, base, 0.42)
    with pytest.raises(MeasurementPending):
        m.results([new_cmd])
