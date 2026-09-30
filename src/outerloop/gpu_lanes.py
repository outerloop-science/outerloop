"""Deployment-owned GPU lanes; targets never choose scheduler flags."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

KERNEL_FLAGS = frozenset(
    {
        "account",
        "partition",
        "gres",
        "time",
        "qos",
        "nice",
        "array",
        "dependency",
        "begin",
        "job-name",
        "output",
        "error",
        "wrap",
        "parsable",
        "chdir",
    }
)


@dataclass(frozen=True)
class GpuLane:
    partition: str
    account: str = ""
    gpu_type: str = ""
    extra: tuple[str, ...] = ()


def gpu_lanes_from_env() -> dict[str, GpuLane]:
    """Read and validate once at each composition root, before submitting work."""
    return parse_gpu_lanes(os.environ.get("OUTERLOOP_GPU_LANES", "{}"))


def parse_gpu_lanes(raw: str) -> dict[str, GpuLane]:
    """Reject malformed config with one setting-labelled startup error."""
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("expected an object mapping owner/repo to lanes")
        lanes = {}
        for target, lane in data.items():
            if not re.fullmatch(r"[^/\s]+/[^/\s]+", target):
                raise ValueError(f"invalid target {target!r}; expected owner/repo")
            if not isinstance(lane, dict) or lane.keys() - {
                "partition",
                "account",
                "gpu_type",
                "extra",
            }:
                raise ValueError(
                    f"{target}: expected a lane object with only partition/account/gpu_type/extra"
                )
            if not isinstance(lane.get("partition"), str) or not lane["partition"].strip():
                raise ValueError(f"{target}: partition must be a nonempty string")
            for key in ("account", "gpu_type"):
                if key in lane and not isinstance(lane[key], str):
                    raise ValueError(f"{target}: {key} must be a string")
            gpu_type = lane.get("gpu_type", "")
            if gpu_type and not re.fullmatch(r"[A-Za-z0-9_.-]+", gpu_type):
                raise ValueError(f"{target}: gpu_type must be a single GPU type")
            extra = lane.get("extra", [])
            if not isinstance(extra, list):
                raise ValueError(f"{target}: extra must be a list of strings")
            for flag in extra:
                if not isinstance(flag, str) or not re.fullmatch(
                    r"--[a-z][a-z0-9-]*=[^\x00\r\n]*", flag
                ):
                    raise ValueError(f"{target}: extra flags must have the form --name=value")
                name = flag[2:].split("=", 1)[0]
                # later flags win in sbatch, so an extra must not repeat one the kernel sets
                # node, task and exclusivity counts multiply a per-node GPU request past
                # what the caps charge, so they are the kernel's too
                if name in KERNEL_FLAGS or name.startswith(
                    ("gpus", "cpus", "mem", "nodes", "ntasks", "exclusive", "tres-per")
                ):
                    raise ValueError(f"{target}: kernel-owned extra flag --{name}")
            lanes[target] = GpuLane(
                lane["partition"], lane.get("account", ""), gpu_type, tuple(extra)
            )
        return lanes
    except (ValueError, TypeError) as exc:
        raise ValueError(f"OUTERLOOP_GPU_LANES: {exc}") from None
