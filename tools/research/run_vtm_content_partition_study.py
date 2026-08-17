"""Prepare, run, and validate the reproducible baseline VTM CU-partition study."""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import platform
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TypeVar

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.data_prep.generate_distortion_sweep import generate_sweep
from tools.visualization.parse_vvenc_qp_trace import parse_trace, write_csv as write_partition_csv
from vvenc_csf.content_partition import ContentPartitionProtocol, StudyJob
from vvenc_csf.core import CommandRunner, files_equal, platform_executable, repo_path
from vvenc_csf.encoding import DecoderRunner, EncodeJob, EncoderRunner, ImageConverter
from vvenc_csf.partitions import summarize_partitions
from vvenc_csf.stimuli import file_sha256


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROTOCOL = ROOT / "configs" / "vtm_content_partition_study.json"
TRACE_RULE = "D_QP:poc==0"
IMPLEMENTATION_FILES = (
    Path(__file__).resolve(),
    ROOT / "tools" / "data_prep" / "generate_distortion_sweep.py",
    ROOT / "tools" / "visualization" / "parse_vvenc_qp_trace.py",
    ROOT / "vvenc_csf" / "content_partition.py",
    ROOT / "vvenc_csf" / "encoding.py",
    ROOT / "vvenc_csf" / "partitions.py",
    ROOT / "vvenc_csf" / "stimuli.py",
)
Result = TypeVar("Result")


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def write_rows(path: Path, rows: Sequence[Mapping[str, object]]) -> None:
    if not rows:
        raise ValueError(f"Cannot write an empty CSV: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames: list[str] = []
    for row in rows:
        fieldnames.extend(key for key in row if key not in fieldnames)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def iter_job_results(
    jobs: Sequence[StudyJob],
    worker: Callable[[StudyJob], Result],
    workers: int,
) -> Iterator[tuple[StudyJob, Result]]:
    if workers == 1:
        for job in jobs:
            yield job, worker(job)
        return

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="vtm")
    futures = {executor.submit(worker, job): job for job in jobs}
    try:
        for future in concurrent.futures.as_completed(futures):
            yield futures[future], future.result()
    except BaseException:
        for future in futures:
            future.cancel()
        executor.shutdown(wait=True, cancel_futures=True)
        raise
    else:
        executor.shutdown(wait=True)


def job_key(row: Mapping[str, object]) -> tuple[str, int, str]:
    return str(row["stimulus"]), int(row["qp"]), str(row.get("mode", "baseline"))


def output_paths(results: Path, row: Mapping[str, object] | StudyJob) -> dict[str, Path]:
    if isinstance(row, StudyJob):
        stimulus, qp, mode = row.stimulus, row.qp, row.mode
    else:
        stimulus, qp, mode = job_key(row)
    run_dir = results / "encoded" / stimulus / f"QP{qp}" / mode
    prefix = f"{stimulus}_QP{qp}_{mode}"
    return {
        "bitstream": run_dir / f"{prefix}.vvc",
        "reconstruction": run_dir / f"{prefix}_rec.yuv",
        "decoded": run_dir / f"{prefix}_dec.yuv",
        "trace": run_dir / f"{prefix}_d_qp.trace",
        "partition_csv": run_dir / f"{prefix}.csv",
        "encoder_log": run_dir / f"{prefix}_enc.log",
        "decoder_log": run_dir / f"{prefix}_dec.log",
    }


def completed_row_is_valid(row: Mapping[str, object], results: Path) -> bool:
    paths = output_paths(results, row)
    for name, path in paths.items():
        expected = str(row.get(f"{name}_sha256", ""))
        if not expected or not path.is_file() or path.stat().st_size == 0:
            return False
        if file_sha256(path) != expected:
            return False
    return files_equal(paths["reconstruction"], paths["decoded"])


@dataclass(frozen=True)
class StudySettings:
    protocol_path: Path
    protocol: ContentPartitionProtocol
    results: Path
    encoder: Path
    decoder: Path
    encoder_config: Path
    workers: int
    force: bool = False


class VTMContentPartitionStudy:
    """Own stimulus preparation, resumable encoding, and final acceptance."""

    def __init__(self, settings: StudySettings) -> None:
        self.settings = settings
        self.protocol = settings.protocol
        self.results = settings.results
        self.manifest_path = self.results / "manifest.csv"
        self.plan_path = self.results / "job_plan.csv"
        self.snapshot_path = self.results / "study_snapshot.json"
        self.summary_path = self.results / "partition_summary.csv"
        self.progress_path = self.results / "progress.json"
        runner = CommandRunner(ROOT)
        self.converter = ImageConverter(runner)
        self.encoder = EncoderRunner(runner)
        self.decoder = DecoderRunner(settings.decoder, runner)

    def prepare(self) -> Path:
        self._preflight(require_study_inputs=False)
        rows = generate_sweep(
            input_dir=self._project_path(self.protocol.source_dir),
            output_dir=self.results / "stimuli",
            dataset=self.protocol.dataset,
            sigmas=self.protocol.awgn_sigmas,
            seeds=self.protocol.awgn_seeds,
            amplitudes=self.protocol.stripe_amplitudes,
            manifest=self.manifest_path,
        )
        portable_rows = [dict(row, path=repo_path(Path(str(row["path"])))) for row in rows]
        self._validate_manifest(portable_rows)
        write_rows(self.manifest_path, portable_rows)
        plan = self.protocol.plan(portable_rows)
        self._validate_plan(plan)
        write_rows(self.plan_path, self._plan_rows(plan))
        write_json(self.snapshot_path, self._snapshot_payload())
        print(f"Prepared {len(portable_rows)} stimuli and {len(plan)} unique jobs.", flush=True)
        return self.manifest_path

    def run(self) -> Path:
        manifest_rows, plan, snapshot = self._load_frozen_inputs()
        manifest_by_stimulus = {row["stimulus"]: row for row in manifest_rows}
        expected_keys = {job.key for job in plan}
        previous = [] if not self.summary_path.exists() else read_rows(self.summary_path)
        valid_previous_by_key = {
            job_key(row): row
            for row in previous
            if job_key(row) in expected_keys and self._completed_row_matches(row, manifest_by_stimulus, snapshot)
        }
        valid_previous = list(valid_previous_by_key.values())
        completed = set() if self.settings.force else {job_key(row) for row in valid_previous}
        rows: list[dict[str, object]] = [] if self.settings.force else [dict(row) for row in valid_previous]
        pending = [job for job in plan if job.key not in completed]
        print(
            f"Study plan: {len(pending)} pending of {len(plan)} jobs; workers={self.settings.workers}.",
            flush=True,
        )

        for stimulus in sorted({job.stimulus for job in pending}):
            self.prepare_yuv(manifest_by_stimulus[stimulus])

        for index, (job, result) in enumerate(
            iter_job_results(
                pending,
                lambda item: self.run_job(item, manifest_by_stimulus[item.stimulus], snapshot),
                self.settings.workers,
            ),
            start=len(completed) + 1,
        ):
            rows.append(result)
            rows.sort(key=job_key)
            write_rows(self.summary_path, rows)
            self._write_progress(len(plan), index, job, complete=index == len(plan))
            print(f"[{index}/{len(plan)}] completed {job.stimulus} QP{job.qp}", flush=True)

        if not pending:
            self._write_progress(len(plan), len(plan), None, complete=True)
        return self.summary_path

    def validate(self) -> Path:
        manifest_rows, plan, snapshot = self._load_frozen_inputs()
        manifest_by_stimulus = {row["stimulus"]: row for row in manifest_rows}
        rows = read_rows(self.summary_path) if self.summary_path.is_file() else []
        expected = {job.key for job in plan}
        actual = [job_key(row) for row in rows]
        unique_actual = set(actual)
        artifacts_valid = bool(rows) and all(
            self._completed_row_matches(row, manifest_by_stimulus, snapshot) for row in rows
        )
        reconstructions_verified = bool(rows) and all(
            _csv_bool(row.get("reconstruction_verified")) for row in rows
        )
        coverage_verified = bool(rows) and all(_csv_bool(row.get("cu_coverage_verified")) for row in rows)
        accepted = (
            len(rows) == len(plan)
            and len(unique_actual) == len(actual)
            and unique_actual == expected
            and artifacts_valid
            and reconstructions_verified
            and coverage_verified
        )
        acceptance_path = self.results / "acceptance.json"
        write_json(
            acceptance_path,
            {
                "accepted": accepted,
                "completed_jobs": len(rows),
                "unique_job_keys": len(unique_actual),
                "expected_jobs": len(plan),
                "all_artifacts_verified": artifacts_valid,
                "all_reconstructions_verified": reconstructions_verified,
                "all_cu_partitions_verified": coverage_verified,
                "protocol_sha256": snapshot["protocol_sha256"],
                "manifest_sha256": snapshot["manifest_sha256"],
                "job_plan_sha256": snapshot["job_plan_sha256"],
                "encoder_sha256": snapshot["encoder_sha256"],
                "decoder_sha256": snapshot["decoder_sha256"],
                "encoder_config_sha256": snapshot["encoder_config_sha256"],
                "implementation_sha256": snapshot["implementation_sha256"],
            },
        )
        if not accepted:
            raise RuntimeError(f"Content-partition acceptance failed: {acceptance_path}")
        return acceptance_path

    def prepare_yuv(self, manifest_row: Mapping[str, str]) -> Path:
        image = self._project_path(Path(manifest_row["path"]))
        if file_sha256(image) != manifest_row["sha256"]:
            raise RuntimeError(f"Stimulus hash no longer matches the manifest: {image}")
        width, height = int(manifest_row["width"]), int(manifest_row["height"])
        yuv = self._yuv_path(manifest_row)
        expected_size = width * height * 3
        if yuv.exists() and yuv.stat().st_size != expected_size:
            yuv.unlink()
        if self.protocol.conversion == "opencv_444":
            self.converter.to_yuv444p_opencv(image, yuv)
        else:
            self.converter.to_yuv444p(image, yuv)
        if not yuv.is_file() or yuv.stat().st_size != expected_size:
            raise RuntimeError(f"Unexpected YUV444 size for {yuv}: expected {expected_size} bytes")
        return yuv

    def run_job(
        self,
        job: StudyJob,
        manifest_row: Mapping[str, str],
        snapshot: Mapping[str, object],
    ) -> dict[str, object]:
        width, height = int(manifest_row["width"]), int(manifest_row["height"])
        yuv = self._yuv_path(manifest_row)
        if not yuv.is_file():
            raise RuntimeError(f"Prepared YUV is missing: {yuv}")
        final_outputs = output_paths(self.results, job)
        partial = {name: path.with_suffix(path.suffix + ".partial") for name, path in final_outputs.items()}
        for path in partial.values():
            path.unlink(missing_ok=True)

        started = time.perf_counter()
        self.encoder.encode(
            EncodeJob(
                encoder=self.settings.encoder,
                yuv=yuv,
                width=width,
                height=height,
                qp=job.qp,
                preset="medium",
                bitstream=partial["bitstream"],
                recon=partial["reconstruction"],
                log=partial["encoder_log"],
                extra_args=(f"--TraceFile={partial['trace']}", f"--TraceRule={TRACE_RULE}"),
                codec="vtm",
                encoder_config=self.settings.encoder_config,
            )
        )
        encode_seconds = time.perf_counter() - started
        self.decoder.decode(partial["bitstream"], partial["decoded"], partial["decoder_log"])
        if not files_equal(partial["reconstruction"], partial["decoded"]):
            raise RuntimeError(f"Decoded YUV differs from encoder reconstruction: {partial['decoded']}")

        partition_rows = parse_trace(partial["trace"], frame=0, mode="baseline")
        write_partition_csv(partition_rows, partial["partition_csv"])
        statistics = summarize_partitions(partition_rows, width, height)
        for name, path in partial.items():
            path.replace(final_outputs[name])

        image = self._project_path(Path(manifest_row["path"]))
        result: dict[str, object] = {
            "dataset": manifest_row["dataset"],
            "source": manifest_row["source"],
            "stimulus": manifest_row["stimulus"],
            "distortion": manifest_row["distortion"],
            "level": manifest_row["level"],
            "seed": manifest_row["seed"],
            "derived_seed": manifest_row.get("derived_seed", ""),
            "actual_luma_rms": manifest_row.get("actual_luma_rms", ""),
            "path": repo_path(image),
            "image_sha256": manifest_row["sha256"],
            "yuv_sha256": file_sha256(yuv),
            "source_sobel_si": manifest_row["source_sobel_si"],
            "stimulus_sobel_si": manifest_row["stimulus_sobel_si"],
            "qp": job.qp,
            "mode": job.mode,
            "analyses": ";".join(job.analyses),
            "conversion": self.protocol.conversion,
            "trace_rule": TRACE_RULE,
            "protocol_sha256": snapshot["protocol_sha256"],
            "manifest_sha256": snapshot["manifest_sha256"],
            "job_plan_sha256": snapshot["job_plan_sha256"],
            "encoder_sha256": snapshot["encoder_sha256"],
            "decoder_sha256": snapshot["decoder_sha256"],
            "encoder_config_sha256": snapshot["encoder_config_sha256"],
            "encode_seconds": encode_seconds,
            "bitstream_bytes": final_outputs["bitstream"].stat().st_size,
            "reconstruction_verified": True,
            "cu_coverage_verified": True,
            **statistics,
        }
        result.update((f"{name}_sha256", file_sha256(path)) for name, path in final_outputs.items())
        return result

    def _load_frozen_inputs(
        self,
    ) -> tuple[list[dict[str, str]], tuple[StudyJob, ...], dict[str, object]]:
        self._preflight(require_study_inputs=True)
        manifest_rows = read_rows(self.manifest_path)
        self._validate_manifest(manifest_rows)
        plan = self.protocol.plan(manifest_rows)
        self._validate_plan(plan)
        expected_plan_rows = self._plan_rows(plan)
        if read_rows(self.plan_path) != [
            {key: str(value) for key, value in row.items()} for row in expected_plan_rows
        ]:
            raise RuntimeError("Frozen job plan does not match the protocol and manifest")
        snapshot = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
        current = self._snapshot_payload()
        if snapshot != current:
            mismatches = sorted(key for key in current if snapshot.get(key) != current[key])
            raise RuntimeError(f"Frozen study provenance mismatch: {', '.join(mismatches)}")
        return manifest_rows, plan, snapshot

    def _completed_row_matches(
        self,
        row: Mapping[str, object],
        manifest_by_stimulus: Mapping[str, Mapping[str, str]],
        snapshot: Mapping[str, object],
    ) -> bool:
        manifest_row = manifest_by_stimulus.get(str(row.get("stimulus", "")))
        if manifest_row is None or str(row.get("mode", "")) != "baseline":
            return False
        for name in ("dataset", "source", "stimulus", "distortion", "level", "seed"):
            if str(row.get(name, "")) != str(manifest_row[name]):
                return False
        checks = {
            "image_sha256": manifest_row["sha256"],
            "conversion": self.protocol.conversion,
            "trace_rule": TRACE_RULE,
            "protocol_sha256": snapshot["protocol_sha256"],
            "manifest_sha256": snapshot["manifest_sha256"],
            "job_plan_sha256": snapshot["job_plan_sha256"],
            "encoder_sha256": snapshot["encoder_sha256"],
            "decoder_sha256": snapshot["decoder_sha256"],
            "encoder_config_sha256": snapshot["encoder_config_sha256"],
        }
        if any(str(row.get(name, "")) != str(expected) for name, expected in checks.items()):
            return False
        yuv = self._yuv_path(manifest_row)
        if not yuv.is_file() or str(row.get("yuv_sha256", "")) != file_sha256(yuv):
            return False
        return (
            _csv_bool(row.get("reconstruction_verified"))
            and _csv_bool(row.get("cu_coverage_verified"))
            and completed_row_is_valid(row, self.results)
        )

    def _snapshot_payload(self) -> dict[str, object]:
        import cv2
        import numpy

        source_dir = self._project_path(self.protocol.source_dir)
        sources = sorted(source_dir.glob("*.png"))
        return {
            "schema_version": 1,
            "protocol": repo_path(self.settings.protocol_path),
            "protocol_sha256": file_sha256(self.settings.protocol_path),
            "manifest": repo_path(self.manifest_path),
            "manifest_sha256": file_sha256(self.manifest_path),
            "job_plan": repo_path(self.plan_path),
            "job_plan_sha256": file_sha256(self.plan_path),
            "source_sha256": {path.name: file_sha256(path) for path in sources},
            "implementation_sha256": {
                repo_path(path): file_sha256(path) for path in IMPLEMENTATION_FILES
            },
            "encoder": repo_path(self.settings.encoder),
            "encoder_sha256": file_sha256(self.settings.encoder),
            "decoder": repo_path(self.settings.decoder),
            "decoder_sha256": file_sha256(self.settings.decoder),
            "encoder_config": repo_path(self.settings.encoder_config),
            "encoder_config_sha256": file_sha256(self.settings.encoder_config),
            "conversion": self.protocol.conversion,
            "trace_rule": TRACE_RULE,
            "python": platform.python_version(),
            "numpy": numpy.__version__,
            "opencv": cv2.__version__,
            "platform": platform.platform(),
        }

    def _validate_manifest(self, rows: Sequence[Mapping[str, object]]) -> None:
        if any(str(row.get("dataset", "")) != self.protocol.dataset for row in rows):
            raise RuntimeError(f"Manifest must contain only the {self.protocol.dataset} dataset")
        sources = {
            str(row["source"])
            for row in rows
        }
        expected_conditions = {("clean", 0.0, "")}
        expected_conditions.update(
            ("awgn", sigma, str(seed))
            for sigma in self.protocol.awgn_sigmas
            for seed in self.protocol.awgn_seeds
        )
        expected_conditions.update(
            ("stripes", amplitude, "") for amplitude in self.protocol.stripe_amplitudes
        )
        if len(sources) != self.protocol.expected_source_count:
            raise RuntimeError(
                f"Expected {self.protocol.expected_source_count} sources, found {len(sources)}"
            )
        for source in sources:
            source_rows = [row for row in rows if str(row["source"]) == source]
            actual_conditions = {
                (str(row["distortion"]), float(row["level"]), str(row.get("seed", "")))
                for row in source_rows
            }
            if len(source_rows) != len(expected_conditions) or actual_conditions != expected_conditions:
                raise RuntimeError(f"Incomplete or unexpected stimulus conditions for {source}")
        stimuli = [str(row["stimulus"]) for row in rows]
        if len(stimuli) != len(set(stimuli)):
            raise RuntimeError("Stimulus identifiers must be unique")

    def _preflight(self, require_study_inputs: bool) -> None:
        required = [
            self.settings.protocol_path,
            self._project_path(self.protocol.source_dir),
            self.settings.encoder,
            self.settings.decoder,
            self.settings.encoder_config,
        ]
        if require_study_inputs:
            required.extend((self.manifest_path, self.plan_path, self.snapshot_path))
        missing = [path for path in required if not path.exists()]
        if missing:
            binary_hint = (
                " Download release binaries with `python tools/data_prep/download_binaries.py`."
                if self.settings.encoder in missing or self.settings.decoder in missing
                else ""
            )
            raise FileNotFoundError(f"Required study input is missing: {missing[0]}.{binary_hint}")
        if self.settings.workers < 1:
            raise ValueError("workers must be at least 1")

    def _validate_plan(self, plan: Sequence[StudyJob]) -> None:
        keys = [job.key for job in plan]
        if len(keys) != self.protocol.expected_job_count or len(set(keys)) != len(keys):
            raise RuntimeError(
                f"Expected {self.protocol.expected_job_count} unique jobs, found {len(set(keys))}"
            )

    def _yuv_path(self, manifest_row: Mapping[str, str]) -> Path:
        return self.results / "yuv" / (
            f"{manifest_row['stimulus']}_{manifest_row['sha256'][:12]}_"
            f"{manifest_row['width']}x{manifest_row['height']}.yuv"
        )

    def _write_progress(self, total: int, completed: int, job: StudyJob | None, complete: bool) -> None:
        write_json(
            self.progress_path,
            {
                "total_jobs": total,
                "completed_jobs": completed,
                "pending_jobs": max(0, total - completed),
                "workers": self.settings.workers,
                "last_stimulus": "" if job is None else job.stimulus,
                "last_qp": 0 if job is None else job.qp,
                "complete": complete,
            },
        )

    @staticmethod
    def _plan_rows(plan: Sequence[StudyJob]) -> list[dict[str, object]]:
        return [
            {
                "stimulus": job.stimulus,
                "qp": job.qp,
                "mode": job.mode,
                "analyses": ";".join(job.analyses),
            }
            for job in plan
        ]

    @staticmethod
    def _project_path(path: Path) -> Path:
        return path if path.is_absolute() else ROOT / path


def _csv_bool(value: object) -> bool:
    return str(value).strip().lower() == "true"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "run", "validate", "all"))
    parser.add_argument("--protocol", type=Path, default=DEFAULT_PROTOCOL)
    parser.add_argument("--results", type=Path)
    parser.add_argument("--encoder", type=Path)
    parser.add_argument("--decoder", type=Path)
    parser.add_argument("--encoder-config", type=Path)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--force", action="store_true", help="Rerun every planned encode without deleting outputs.")
    return parser.parse_args()


def settings_from_args(args: argparse.Namespace) -> StudySettings:
    protocol_path = args.protocol if args.protocol.is_absolute() else ROOT / args.protocol
    protocol = ContentPartitionProtocol.from_json(protocol_path)
    if args.results is not None:
        protocol = replace(protocol, results_dir=args.results)
    if args.encoder is not None:
        protocol = replace(protocol, encoder=args.encoder)
    if args.decoder is not None:
        protocol = replace(protocol, decoder=args.decoder)
    if args.encoder_config is not None:
        protocol = replace(protocol, encoder_config=args.encoder_config)

    def project_path(path: Path) -> Path:
        return path if path.is_absolute() else ROOT / path

    return StudySettings(
        protocol_path=protocol_path,
        protocol=protocol,
        results=project_path(protocol.results_dir),
        encoder=platform_executable(project_path(protocol.encoder)),
        decoder=platform_executable(project_path(protocol.decoder)),
        encoder_config=project_path(protocol.encoder_config),
        workers=args.workers,
        force=args.force,
    )


def main() -> int:
    args = parse_args()
    study = VTMContentPartitionStudy(settings_from_args(args))
    if args.action in {"prepare", "all"}:
        study.prepare()
    if args.action in {"run", "all"}:
        study.run()
    if args.action in {"validate", "all"}:
        acceptance = study.validate()
        print(f"Accepted study: {acceptance}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
