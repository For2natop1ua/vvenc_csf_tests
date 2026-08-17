from __future__ import annotations

import csv
from pathlib import Path

import pytest

from tools.reporting.report_vtm_content_partition_study import (
    AWGN_LEVELS,
    AWGN_SEEDS,
    CLEAN_QPS,
    DISTURBANCE_QPS,
    STRIPE_LEVELS,
    ContentPartitionReporter,
)


def measurement(
    source_index: int,
    distortion: str,
    level: int,
    seed: str,
    qp: int,
    cu_count: int,
) -> dict[str, str]:
    source = f"kodim{source_index + 1:02d}.png"
    suffix = f"{distortion}_{level}" + (f"_seed-{seed}" if seed else "")
    return {
        "dataset": "kodak",
        "source": source,
        "stimulus": f"kodim{source_index + 1:02d}__{suffix}",
        "distortion": distortion,
        "level": str(level),
        "seed": seed,
        "derived_seed": seed,
        "actual_luma_rms": str(level),
        "source_sobel_si": str(50 + source_index),
        "qp": str(qp),
        "mode": "baseline",
        "reconstruction_verified": "True",
        "cu_coverage_verified": "True",
        "cu_count": str(cu_count),
        "coded_area": "393216",
        "mean_area": str(393216 / cu_count),
        "median_area": "64",
        "mean_equivalent_side": "8",
        "cu_density_per_mpixel": str(cu_count / 0.393216),
        "min_area": "16",
        "max_area": "4096",
        "dominant_sizes": "8x8:1",
    }


def complete_rows() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for source_index in range(24):
        for qp in CLEAN_QPS:
            rows.append(measurement(source_index, "clean", 0, "", qp, 1000 - qp))
        for qp in DISTURBANCE_QPS:
            for sigma in AWGN_LEVELS:
                rows.append(
                    measurement(
                        source_index,
                        "awgn",
                        sigma,
                        AWGN_SEEDS[0],
                        qp,
                        1000 + sigma + source_index,
                    )
                )
            for amplitude in STRIPE_LEVELS:
                rows.append(
                    measurement(
                        source_index,
                        "stripes",
                        amplitude,
                        "",
                        qp,
                        900 + amplitude + source_index,
                    )
                )
        for sigma in AWGN_LEVELS:
            for seed_index, seed in enumerate(AWGN_SEEDS[1:], start=1):
                rows.append(
                    measurement(
                        source_index,
                        "awgn",
                        sigma,
                        seed,
                        32,
                        1000 + sigma + source_index + seed_index,
                    )
                )
    return rows


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


@pytest.fixture
def reporter(tmp_path: Path) -> ContentPartitionReporter:
    instance = ContentPartitionReporter(tmp_path / "results", tmp_path / "docs")
    instance.rows = complete_rows()
    return instance


def test_reporter_selects_each_complete_scientific_matrix(
    reporter: ContentPartitionReporter,
) -> None:
    assert len(reporter.clean_measurements()) == 96
    assert len(reporter.disturbance_measurements()) == 504
    assert len(reporter.realization_measurements()) == 216

    reporter.rows.pop()
    with pytest.raises(RuntimeError, match="complete"):
        reporter.realization_measurements()


def test_realization_variability_reports_exact_mean_normalized_range(
    reporter: ContentPartitionReporter,
) -> None:
    rows = reporter.realization_measurements()
    cells = reporter.realization_variability(rows)

    first = cells[0]
    assert len(cells) == 72
    assert first["min_cu_count"] == 1005
    assert first["max_cu_count"] == 1007
    assert first["cu_count_range"] == 2
    assert first["relative_cu_count_range_percent"] == pytest.approx(200 / 1006)


def test_reporter_resolves_manifest_paths_inside_the_selected_results_root(
    tmp_path: Path,
) -> None:
    results = tmp_path / "portable-results"
    summary_row = measurement(0, "clean", 0, "", 22, 1000)
    manifest_row = {
        "stimulus": summary_row["stimulus"],
        "path": "D:/old-machine/results/stimuli/kodim01__clean_0.png",
    }
    write_csv(results / "partition_summary.csv", [summary_row])
    write_csv(results / "manifest.csv", [manifest_row])

    reporter = ContentPartitionReporter(results, tmp_path / "docs")
    reporter.load()

    assert reporter.stimulus_path(summary_row["stimulus"]) == (
        results / "stimuli" / "kodim01__clean_0.png"
    )
    assert reporter.partition_path(summary_row) == (
        results
        / "encoded"
        / summary_row["stimulus"]
        / "QP22"
        / "baseline"
        / f"{summary_row['stimulus']}_QP22_baseline.csv"
    )


def test_reporter_rejects_unverified_cu_coverage(tmp_path: Path) -> None:
    results = tmp_path / "results"
    summary_row = measurement(0, "clean", 0, "", 22, 1000)
    summary_row["cu_coverage_verified"] = "False"
    write_csv(results / "partition_summary.csv", [summary_row])
    write_csv(
        results / "manifest.csv",
        [{"stimulus": summary_row["stimulus"], "path": "stimuli/clean.png"}],
    )

    with pytest.raises(RuntimeError, match="CU coverage"):
        ContentPartitionReporter(results, tmp_path / "docs").load()


def test_realization_plot_uses_png_only(
    reporter: ContentPartitionReporter,
    tmp_path: Path,
) -> None:
    cells = reporter.realization_variability(reporter.realization_measurements())
    output = tmp_path / "realization_variability.png"

    reporter.plot_realization_variability(cells, output)

    assert output.is_file()
    assert not output.with_suffix(".svg").exists()
    assert not output.with_suffix(".pdf").exists()


def test_disturbance_plots_use_png_only(
    reporter: ContentPartitionReporter,
    tmp_path: Path,
) -> None:
    rows = reporter.disturbance_measurements()
    response = tmp_path / "disturbance_response.png"
    heatmap = tmp_path / "disturbance_heatmap.png"

    reporter.plot_disturbance_responses(rows, response)
    reporter.plot_disturbance_heatmap(rows, heatmap)

    assert response.is_file()
    assert heatmap.is_file()
    assert not list(tmp_path.glob("*.svg"))
    assert not list(tmp_path.glob("*.pdf"))
