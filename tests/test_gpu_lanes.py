"""Deployment lane validation and the actual eval/author submission seams."""

import json
from dataclasses import replace

import pytest

from outerloop.attempt import LAUNCH_NICE, _make_launcher, with_seed
from outerloop.compute import CommandResult, JobSpec, SlurmCompute, gpus_in_gres
from outerloop.gpu_lanes import GpuLane, parse_gpu_lanes
from outerloop.measure import DispatchSettings, Measure
from outerloop.syscall import Launch, SyscallRequest

RAW = json.dumps(
    {
        "owner/repo": {
            "partition": "gpu-large",
            "account": "lab-account",
            "gpu_type": "a100",
            "extra": ["--comment=reserved"],
        }
    }
)


def settings(submitted):
    def runner(argv, timeout_s):
        submitted.append(argv)
        return CommandResult(0, "123\n", "")

    return DispatchSettings(
        SlurmCompute(runner=runner),
        "/image.sif",
        "cpu-account",
        "cpu",
        gpu_partition="gpu-fleet",
        gpu_account="fleet-account",
        gpu_lanes=parse_gpu_lanes(RAW),
        target="owner/repo",
    )


def test_parse_valid():
    assert parse_gpu_lanes(RAW) == {
        "owner/repo": GpuLane("gpu-large", "lab-account", "a100", ("--comment=reserved",))
    }
    assert parse_gpu_lanes('{"o/r":{"partition":"gpu-large"}}') == {"o/r": GpuLane("gpu-large")}
    assert parse_gpu_lanes("{}") == {}


@pytest.mark.parametrize(
    "raw",
    [
        "{bad",
        "[]",
        '{"o/r":{"partition":"gpu-large","typo":"x"}}',
        '{"o/r":{"account":"a"}}',
        '{"o/r":{"partition":4}}',
        '{"o/r":{"partition":"p","account":null}}',
        '{"o/r":{"partition":"p","gpu_type":4}}',
        '{"o/r":{"partition":"p","extra":"--comment=x"}}',
        '{"o/r":{"partition":"p","extra":[4]}}',
        '{"o/r":{"partition":"p","gpu_type":"a100:8"}}',
        '{"repo":{"partition":"p"}}',
    ],
)
def test_parse_invalid(raw):
    with pytest.raises(ValueError, match=r"^OUTERLOOP_GPU_LANES:"):
        parse_gpu_lanes(raw)


@pytest.mark.parametrize(
    "flag",
    [
        "--account=a",
        "--partition=p",
        "--gres=gpu:8",
        "--gpus=8",
        "--gpus-per-node=8",
        "--time=99",
        "--mem=2G",
        "--cpus-per-task=8",
        "--output=/tmp/x",
        "--dependency=afterok:1",
        "--qos=high",
        "--nice=0",
        "--mem-per-gpu=10G",
        "--comment",
        "-A=a",
        "--comment=x\n--account=a",
        "--nodes=2",
        "--ntasks-per-node=4",
        "--exclusive=user",
        "--tres-per-task=gres/gpu:2",
    ],
)
def test_parse_forbidden_extra(flag):
    with pytest.raises(ValueError, match=r"^OUTERLOOP_GPU_LANES:"):
        parse_gpu_lanes(json.dumps({"o/r": {"partition": "p", "extra": [flag]}}))


def test_placement_and_resume(tmp_path):
    d = settings([])
    assert d.placement(2) == ("lab-account", "gpu-large")
    assert d.placement(0) == ("cpu-account", "cpu")
    assert d.lane(0).extra == ()
    assert replace(d, target="owner/other-repo").placement(2) == ("fleet-account", "gpu-fleet")
    assert replace(d, gpu_partition="").placement(1) == ("lab-account", "gpu-large")
    minimal = replace(d, gpu_lanes={"owner/repo": GpuLane("gpu-large")})
    assert minimal.placement(1) == ("cpu-account", "gpu-large")
    # Wake CLI has only a run id; bind the persisted target even with a seed override.
    wake = replace(d, target="", seed_cache=tmp_path)
    assert with_seed(wake, tmp_path, "owner/repo").lane(1) == d.lane(1)


def assert_lane(argv):
    assert "--partition=gpu-large" in argv
    assert "--account=lab-account" in argv
    assert "--gres=gpu:a100:2" in argv
    assert "--comment=reserved" in argv
    assert not any(arg.startswith("--gpus") for arg in argv)


def test_eval_and_author_array_submissions(tmp_path):
    submitted: list[list[str]] = []
    d = settings(submitted)
    measurer = d.measurer(tmp_path, tmp_path / "repo", 10, "run")
    measurer._dispatch(Measure("candidate", "a" * 40, "true", "score", gpus=2))
    assert_lane(submitted[-1])
    assert not any(arg.startswith("--nice") for arg in submitted[-1])
    launcher = _make_launcher(d, tmp_path, tmp_path / "repo", "run", gpus=2)
    request = SyscallRequest((Launch("probe", "true", 10, array=4, concurrency=2),))
    assert launcher("a" * 40, request) == "afterany:123"
    assert_lane(submitted[-1])
    assert "--array=0-3%2" in submitted[-1]
    assert f"--nice={LAUNCH_NICE}" in submitted[-1]


def test_unlaned_author_golden_argv(tmp_path):
    submitted: list[list[str]] = []
    d = replace(settings(submitted), target="owner/other-repo")
    _make_launcher(d, tmp_path, tmp_path / "repo", "run", gpus=2)(
        "a" * 40, SyscallRequest((Launch("probe", "true", 10),))
    )
    assert submitted[-1] == [
        "sbatch",
        "--parsable",
        "--job-name=run-launch-probe",
        "--time=20",
        "--cpus-per-task=16",
        "--mem=128G",
        "--output=/dev/null",
        "--account=fleet-account",
        "--partition=gpu-fleet",
        "--gpus-per-node=2",
        "--nice=5000",
        str(tmp_path / "eval-launch-probe" / "job.sh"),
    ]


def test_typed_gres_and_legacy_argv():
    spec = JobSpec("j", "a", "p", 10, command="true", gpus=2)
    assert spec.to_argv() == [
        "sbatch",
        "--parsable",
        "--job-name=j",
        "--time=10",
        "--cpus-per-task=1",
        "--mem=2G",
        "--output=/dev/null",
        "--account=a",
        "--partition=p",
        "--gpus-per-node=2",
        "--wrap=true",
    ]
    assert "--gres=gpu:a100:2" in replace(spec, gpu_type="a100").to_argv()
    assert gpus_in_gres("gres/gpu:a100:2") == 2


@pytest.mark.parametrize("module", ["outerloop.tick", "outerloop.attempt"])
def test_bad_json_is_one_startup_error(monkeypatch, capsys, tmp_path, module):
    import importlib

    monkeypatch.setenv("OUTERLOOP_GPU_LANES", "{bad")
    monkeypatch.setattr(
        "sys.argv",
        [module, "--root" if module == "outerloop.tick" else "--run-root", str(tmp_path)],
    )
    with pytest.raises(SystemExit) as exc:
        importlib.import_module(module).main()
    assert exc.value.code == 2
    error = capsys.readouterr().err
    assert error.count("OUTERLOOP_GPU_LANES:") == 1
    assert "Traceback" not in error


def test_cpu_submission_ignores_target_gpu_flags(tmp_path):
    submitted: list[list[str]] = []
    d = settings(submitted)
    d.measurer(tmp_path, tmp_path / "repo", 10, "run")._dispatch(
        Measure("cpu", "a" * 40, "true", "score")
    )
    argv = submitted[-1]
    assert "--account=cpu-account" in argv and "--partition=cpu" in argv
    assert not any(a.startswith(("--gres", "--gpus", "--comment")) for a in argv)


def test_tick_preflight_accepts_override_without_fleet_lane(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from outerloop.tick import _gpu_lane_error, _service_spec_from_env

    image = tmp_path / "image.sif"
    image.touch()
    pat = tmp_path / "pat"
    pat.write_text("test-token")
    monkeypatch.setattr(
        "os.environ",
        {
            "OUTERLOOP_PAT_FILE": str(pat),
            "OUTERLOOP_IMAGE": str(image),
            "OUTERLOOP_HOME": str(tmp_path),
            "OUTERLOOP_TARGET": "owner/repo",
            "OUTERLOOP_BOT_LOGIN": "bot",
            "OUTERLOOP_GPU_LANES": RAW,
        },
    )
    _, spec = _service_spec_from_env(tmp_path)
    assert spec is not None
    assert spec.gpu_partition == "" and spec.gpu_account == ""
    assert spec.gpu_lanes["owner/repo"].partition == "gpu-large"
    contract = SimpleNamespace(benchmarks=[SimpleNamespace(name="bench", gpus=2)])
    assert _gpu_lane_error(contract, "bench", spec) == ""

    # The legacy wake recipe stays byte-for-byte the same with overrides configured.
    from outerloop.tick import WAKE_SPEC_NAME, load_wake_spec, write_wake_spec

    write_wake_spec(tmp_path, replace(spec, gpu_lanes={}))
    legacy = (tmp_path / WAKE_SPEC_NAME).read_bytes()
    write_wake_spec(tmp_path, spec)
    assert (tmp_path / WAKE_SPEC_NAME).read_bytes() == legacy
    restored = load_wake_spec(tmp_path)
    assert restored is not None and restored.gpu_lanes == {}
