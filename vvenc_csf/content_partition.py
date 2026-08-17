"""Protocol and deterministic job planning for the VTM CU-partition study."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


def _positive_ints(values: Sequence[object], name: str) -> tuple[int, ...]:
    parsed = tuple(int(value) for value in values)
    if not parsed or any(value <= 0 for value in parsed) or len(set(parsed)) != len(parsed):
        raise ValueError(f"{name} must contain unique positive integers")
    return parsed


def _positive_floats(values: Sequence[object], name: str) -> tuple[float, ...]:
    parsed = tuple(float(value) for value in values)
    if not parsed or any(value <= 0 for value in parsed) or len(set(parsed)) != len(parsed):
        raise ValueError(f"{name} must contain unique positive values")
    return parsed


@dataclass(frozen=True, order=True)
class StudyJob:
    """One unique baseline VTM encode and the analyses that consume it."""

    stimulus: str
    qp: int
    analyses: tuple[str, ...]

    @property
    def mode(self) -> str:
        return "baseline"

    @property
    def key(self) -> tuple[str, int, str]:
        return self.stimulus, self.qp, self.mode


@dataclass(frozen=True)
class ContentPartitionProtocol:
    """Validated configuration and job planner for the baseline-only study."""

    dataset: str
    source_dir: Path
    results_dir: Path
    encoder: Path
    decoder: Path
    encoder_config: Path
    conversion: str
    complexity_qps: tuple[int, ...]
    interference_qps: tuple[int, ...]
    realization_qp: int
    awgn_sigmas: tuple[float, ...]
    awgn_seeds: tuple[int, ...]
    interference_seed: int
    awgn_generator: str
    awgn_image_seed: str
    stripe_amplitudes: tuple[float, ...]
    stripe_period_pixels: int
    stripe_phase: float
    stripe_orientation: str
    expected_source_count: int

    @property
    def expected_jobs_per_source(self) -> int:
        clean = len(self.complexity_qps)
        awgn = len(self.awgn_sigmas) * (
            len(self.interference_qps) + len(self.awgn_seeds) - 1
        )
        stripes = len(self.stripe_amplitudes) * len(self.interference_qps)
        return clean + awgn + stripes

    @property
    def expected_job_count(self) -> int:
        return self.expected_source_count * self.expected_jobs_per_source

    @classmethod
    def from_json(cls, path: Path) -> "ContentPartitionProtocol":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if int(payload.get("schema_version", 0)) != 1:
            raise ValueError("Unsupported content-partition protocol schema")
        awgn = _mapping(payload, "awgn")
        stripes = _mapping(payload, "stripes")
        protocol = cls(
            dataset=str(payload["dataset"]),
            source_dir=Path(payload["source_dir"]),
            results_dir=Path(payload["results_dir"]),
            encoder=Path(payload["encoder"]),
            decoder=Path(payload["decoder"]),
            encoder_config=Path(payload["encoder_config"]),
            conversion=str(payload["conversion"]),
            complexity_qps=_positive_ints(payload["complexity_qps"], "complexity_qps"),
            interference_qps=_positive_ints(payload["interference_qps"], "interference_qps"),
            realization_qp=int(payload["realization_qp"]),
            awgn_sigmas=_positive_floats(awgn["sigmas"], "awgn.sigmas"),
            awgn_seeds=_positive_ints(awgn["seeds"], "awgn.seeds"),
            interference_seed=int(awgn["interference_seed"]),
            awgn_generator=str(awgn["generator"]),
            awgn_image_seed=str(awgn["image_seed"]),
            stripe_amplitudes=_positive_floats(stripes["amplitudes"], "stripes.amplitudes"),
            stripe_period_pixels=int(stripes["period_pixels"]),
            stripe_phase=float(stripes["phase"]),
            stripe_orientation=str(stripes["orientation"]),
            expected_source_count=int(payload["expected_source_count"]),
        )
        protocol._validate()
        return protocol

    def _validate(self) -> None:
        if not self.dataset:
            raise ValueError("dataset must not be empty")
        if self.conversion not in {"opencv_444", "ffmpeg_444"}:
            raise ValueError("conversion must be opencv_444 or ffmpeg_444")
        if self.expected_source_count <= 0:
            raise ValueError("expected_source_count must be positive")
        if self.realization_qp <= 0:
            raise ValueError("realization_qp must be positive")
        if self.interference_seed not in self.awgn_seeds:
            raise ValueError("awgn.interference_seed must be present in awgn.seeds")
        if self.awgn_generator != "numpy.random.PCG64":
            raise ValueError("Only numpy.random.PCG64 AWGN generation is supported")
        if self.awgn_image_seed != "SeedSequence([base_seed, zero_based_sorted_source_index])":
            raise ValueError("Unsupported per-image AWGN seed derivation")
        if not set(self.interference_qps).issubset(self.complexity_qps):
            raise ValueError("interference_qps must be a subset of complexity_qps")
        if self.realization_qp not in self.interference_qps:
            raise ValueError("realization_qp must be present in interference_qps")
        if (
            self.stripe_period_pixels != 16
            or self.stripe_phase != 0.0
            or self.stripe_orientation != "horizontal"
        ):
            raise ValueError("Only zero-phase horizontal sinusoidal interference with a 16-pixel period is supported")

    def plan(self, manifest_rows: Sequence[Mapping[str, object]]) -> tuple[StudyJob, ...]:
        """Return the deduplicated baseline job plan for the three analyses."""

        planned: dict[tuple[str, int], set[str]] = {}

        def add(stimulus: str, qp: int, analysis: str) -> None:
            planned.setdefault((stimulus, qp), set()).add(analysis)

        for row in manifest_rows:
            if str(row.get("dataset", "")) != self.dataset:
                continue
            stimulus = str(row["stimulus"])
            distortion = str(row["distortion"])
            level = float(row["level"])
            seed_text = str(row.get("seed", ""))

            if distortion == "clean" and level == 0:
                for qp in self.complexity_qps:
                    add(stimulus, qp, "complexity_qp")
                    if qp in self.interference_qps:
                        add(stimulus, qp, "interference_qp")
                continue

            if distortion == "awgn" and level in self.awgn_sigmas and seed_text:
                seed = int(seed_text)
                if seed not in self.awgn_seeds:
                    continue
                add(stimulus, self.realization_qp, "awgn_realizations")
                if seed == self.interference_seed:
                    for qp in self.interference_qps:
                        add(stimulus, qp, "interference_qp")
                continue

            if distortion == "stripes" and level in self.stripe_amplitudes and not seed_text:
                for qp in self.interference_qps:
                    add(stimulus, qp, "interference_qp")

        return tuple(
            StudyJob(stimulus, qp, tuple(sorted(analyses)))
            for (stimulus, qp), analyses in sorted(planned.items())
        )


def _mapping(payload: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = payload[name]
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value
