from __future__ import annotations

import csv
import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs" / "vtm_content_partition_study"
README = DOCS / "README.md"
TABLES = DOCS / "tables"
SOURCES = {f"kodim{index:02d}.png" for index in range(1, 25)}


def read_csv(name: str) -> list[dict[str, str]]:
    with (TABLES / name).open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def test_study_readme_uses_only_reproducible_canonical_references() -> None:
    text = README.read_text(encoding="utf-8")
    lowered = text.lower()

    assert "../../results/" not in lowered
    assert not re.search(r"\btask[ _-]?[123]\b|completion|confirmatory|qp[ _-]extension|pilot", lowered)
    assert not re.search(r"\.(?:svg|pdf)(?:\)|[\"'])", lowered)
    assert (
        "[decoded-output verification in the study runner]"
        "(../../tools/research/run_vtm_content_partition_study.py#L313-L315)"
    ) in text
    assert "[CU coverage validation](../../vvenc_csf/partitions.py#L36-L56)" in text


def test_study_readme_documents_the_fresh_clone_workflow() -> None:
    text = README.read_text(encoding="utf-8")

    commands = (
        "python tools/data_prep/download_binaries.py",
        "python tools/research/run_vtm_content_partition_study.py all",
        "python tools/reporting/report_vtm_content_partition_study.py",
    )
    positions = [text.index(command) for command in commands]
    assert positions == sorted(positions)
    assert "Intermediate bitstreams, reconstructions, traces, and progress files are written under `results/`." in text
    assert "do not need to be committed" not in text


def test_clean_measurements_cover_every_source_and_qp_once() -> None:
    rows = read_csv("clean_cu_measurements.csv")

    keys = {(row["source"], int(row["qp"])) for row in rows}
    assert len(rows) == len(keys) == 96
    assert {row["source"] for row in rows} == SOURCES
    assert {int(row["qp"]) for row in rows} == {22, 27, 32, 37}
    for row in rows:
        assert float(row["mean_area"]) == pytest.approx(
            float(row["coded_area"]) / int(row["cu_count"])
        )


def test_interference_measurements_cover_the_registered_matrix_once() -> None:
    rows = read_csv("interference_cu_measurements.csv")

    keys = {
        (row["source"], row["distortion"], row["level"], row["seed"], int(row["qp"]))
        for row in rows
    }
    conditions = {
        (row["distortion"], float(row["level"]), row["seed"])
        for row in rows
    }
    assert len(rows) == len(keys) == 504
    assert {row["source"] for row in rows} == SOURCES
    assert {int(row["qp"]) for row in rows} == {22, 32, 37}
    assert conditions == {
        ("clean", 0.0, ""),
        ("awgn", 5.0, "20260811"),
        ("awgn", 15.0, "20260811"),
        ("awgn", 30.0, "20260811"),
        ("stripes", 8.0, ""),
        ("stripes", 16.0, ""),
        ("stripes", 32.0, ""),
    }
    for row in rows:
        assert float(row["mean_area"]) == pytest.approx(393_216 / int(row["cu_count"]))


def test_awgn_realization_tables_cover_every_registered_cell() -> None:
    measurements = read_csv("awgn_realization_cu_measurements.csv")
    variability = read_csv("awgn_realization_variability.csv")

    measurement_keys = {
        (row["source"], int(row["sigma"]), row["seed"], int(row["qp"]))
        for row in measurements
    }
    variability_keys = {(row["source"], int(row["sigma"])) for row in variability}
    assert len(measurements) == len(measurement_keys) == 216
    assert {row["source"] for row in measurements} == SOURCES
    assert {int(row["sigma"]) for row in measurements} == {5, 15, 30}
    assert {row["seed"] for row in measurements} == {"20260811", "20260812", "20260813"}
    assert {int(row["qp"]) for row in measurements} == {32}
    assert len(variability) == len(variability_keys) == 72
    assert {row["source"] for row in variability} == SOURCES
    assert {int(row["sigma"]) for row in variability} == {5, 15, 30}
    assert {int(row["seed_count"]) for row in variability} == {3}
