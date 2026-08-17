from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

import pytest

from tools.research.run_vtm_content_partition_study import (
    StudySettings,
    VTMContentPartitionStudy,
    completed_row_is_valid,
    iter_job_results,
    job_key,
    output_paths,
)
from vvenc_csf.content_partition import ContentPartitionProtocol, StudyJob
from vvenc_csf.stimuli import file_sha256


ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_PATH = ROOT / "configs" / "vtm_content_partition_study.json"


def _manifest_row(
    source: str,
    stimulus: str,
    distortion: str,
    level: float,
    seed: int | None,
) -> dict[str, object]:
    return {
        "dataset": "kodak",
        "source": source,
        "stimulus": stimulus,
        "distortion": distortion,
        "level": level,
        "seed": "" if seed is None else seed,
    }


def _full_manifest() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for source_index in range(1, 25):
        source = f"kodim{source_index:02d}.png"
        prefix = f"kodim{source_index:02d}"
        rows.append(_manifest_row(source, f"{prefix}-clean", "clean", 0, None))
        for sigma in (5, 15, 30):
            for seed in (20260811, 20260812, 20260813):
                rows.append(
                    _manifest_row(source, f"{prefix}-awgn-{sigma}-{seed}", "awgn", sigma, seed)
                )
        for amplitude in (8, 16, 32):
            rows.append(
                _manifest_row(source, f"{prefix}-stripes-{amplitude}", "stripes", amplitude, None)
            )
    return rows


def test_production_protocol_builds_one_deduplicated_baseline_plan() -> None:
    protocol = ContentPartitionProtocol.from_json(PROTOCOL_PATH)

    plan = protocol.plan(_full_manifest())

    assert protocol.expected_job_count == 672
    assert len(plan) == protocol.expected_job_count
    assert len({job.key for job in plan}) == 672
    assert {job.mode for job in plan} == {"baseline"}


def test_plan_records_every_analysis_that_reuses_a_job() -> None:
    protocol = ContentPartitionProtocol.from_json(PROTOCOL_PATH)
    rows = [
        _manifest_row("kodim01.png", "clean", "clean", 0, None),
        _manifest_row("kodim01.png", "awgn-primary", "awgn", 5, 20260811),
        _manifest_row("kodim01.png", "awgn-repeat", "awgn", 5, 20260812),
        _manifest_row("kodim01.png", "stripes", "stripes", 8, None),
    ]

    plan = {job.key: job for job in protocol.plan(rows)}

    assert plan[("clean", 32, "baseline")].analyses == ("complexity_qp", "interference_qp")
    assert plan[("awgn-primary", 32, "baseline")].analyses == (
        "awgn_realizations",
        "interference_qp",
    )
    assert plan[("awgn-repeat", 32, "baseline")].analyses == ("awgn_realizations",)
    assert plan[("stripes", 22, "baseline")].analyses == ("interference_qp",)


def test_protocol_rejects_an_interference_seed_outside_registered_seeds(tmp_path) -> None:
    text = PROTOCOL_PATH.read_text(encoding="utf-8").replace(
        '"interference_seed": 20260811', '"interference_seed": 7'
    )
    path = tmp_path / "protocol.json"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(ValueError, match="interference_seed"):
        ContentPartitionProtocol.from_json(path)


def test_manifest_validation_rejects_a_duplicate_and_missing_condition(tmp_path) -> None:
    protocol = ContentPartitionProtocol.from_json(PROTOCOL_PATH)
    rows = _full_manifest()
    malformed = next(
        row
        for row in rows
        if row["source"] == "kodim01.png"
        and row["distortion"] == "stripes"
        and row["level"] == 32
    )
    malformed["level"] = 16
    settings = StudySettings(
        protocol_path=PROTOCOL_PATH,
        protocol=protocol,
        results=tmp_path,
        encoder=tmp_path / "EncoderApp",
        decoder=tmp_path / "DecoderApp",
        encoder_config=tmp_path / "encoder.cfg",
        workers=1,
    )

    with pytest.raises(RuntimeError, match="Incomplete or unexpected stimulus conditions"):
        VTMContentPartitionStudy(settings)._validate_manifest(rows)


def test_job_key_and_output_paths_are_baseline_only(tmp_path) -> None:
    job = StudyJob("clean", 32, ("complexity_qp",))

    assert job_key({"stimulus": "clean", "qp": "32", "mode": "baseline"}) == job.key
    assert output_paths(tmp_path, job)["trace"] == (
        tmp_path / "encoded" / "clean" / "QP32" / "baseline" / "clean_QP32_baseline_d_qp.trace"
    )


def test_completed_row_requires_nonempty_hash_verified_outputs(tmp_path) -> None:
    row: dict[str, object] = {"stimulus": "clean", "qp": "32", "mode": "baseline"}
    paths = output_paths(tmp_path, row)
    for name, path in paths.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        content = b"reconstruction-content" if name in {"reconstruction", "decoded"} else f"{name}-content".encode()
        path.write_bytes(content)
        row[f"{name}_sha256"] = file_sha256(path)

    assert completed_row_is_valid(row, tmp_path)
    row.pop("trace_sha256")
    assert not completed_row_is_valid(row, tmp_path)


def test_completed_row_rejects_different_reconstruction_and_decoded_bytes(tmp_path) -> None:
    row: dict[str, object] = {"stimulus": "clean", "qp": "32", "mode": "baseline"}
    paths = output_paths(tmp_path, row)
    for name, path in paths.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        content = b"decoded-content" if name == "decoded" else b"other-content"
        path.write_bytes(content)
        row[f"{name}_sha256"] = file_sha256(path)

    assert not completed_row_is_valid(row, tmp_path)


def test_iter_job_results_uses_requested_parallelism() -> None:
    barrier = threading.Barrier(2)
    jobs = [
        StudyJob("a", 22, ("complexity_qp",)),
        StudyJob("b", 22, ("complexity_qp",)),
    ]

    def worker(job: StudyJob) -> str:
        barrier.wait(timeout=2)
        return f"{job.stimulus}-{job.qp}"

    completed = list(iter_job_results(jobs, worker, workers=2))

    assert {result for _, result in completed} == {"a-22", "b-22"}


def test_iter_job_results_propagates_worker_failure() -> None:
    job = StudyJob("a", 22, ("complexity_qp",))

    def worker(item: StudyJob) -> str:
        raise RuntimeError(f"failed {item.stimulus}")

    with pytest.raises(RuntimeError, match="failed a"):
        list(iter_job_results([job], worker, workers=2))


def test_cli_help_lists_the_reproducible_study_actions() -> None:
    script = ROOT / "tools" / "research" / "run_vtm_content_partition_study.py"

    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "{prepare,run,validate,all}" in result.stdout
