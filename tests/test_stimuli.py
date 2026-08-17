from __future__ import annotations

import csv
import hashlib
from pathlib import Path

import cv2
import numpy as np
import pytest

from tools.data_prep.generate_distortion_sweep import MANIFEST_FIELDS, generate_sweep
from vvenc_csf.stimuli import (
    add_achromatic_awgn,
    add_sinusoidal_interference,
    complexity_metrics,
    derive_image_seed,
)


def test_awgn_is_deterministic_and_achromatic() -> None:
    image = np.full((128, 128, 3), (96, 128, 160), dtype=np.uint8)

    first = add_achromatic_awgn(image, sigma=10.0, seed=42)
    repeated = add_achromatic_awgn(image, sigma=10.0, seed=42)
    different = add_achromatic_awgn(image, sigma=10.0, seed=43)

    assert np.array_equal(first, repeated)
    assert not np.array_equal(first, different)
    assert np.array_equal(first[:, :, 1].astype(int) - first[:, :, 0], np.full((128, 128), 32))
    assert np.array_equal(first[:, :, 2].astype(int) - first[:, :, 1], np.full((128, 128), 32))
    assert np.std(first[:, :, 0].astype(float) - image[:, :, 0]) == pytest.approx(10.0, abs=0.2)


def test_sinusoidal_interference_has_fixed_period_and_preserves_alpha() -> None:
    image = np.full((32, 24, 4), 128, dtype=np.uint8)
    image[:, :, 3] = np.arange(32, dtype=np.uint8)[:, None]

    result = add_sinusoidal_interference(image, amplitude=20.0)

    assert np.array_equal(result[0, :, :3], result[16, :, :3])
    assert np.all(result[4, :, :3] == 148)
    assert np.all(result[12, :, :3] == 108)
    assert np.array_equal(result[:, :, 3], image[:, :, 3])


def test_complexity_metrics_distinguish_flat_and_edge_rich_images() -> None:
    flat = np.full((64, 64), 128, dtype=np.uint8)
    stripes = np.tile(np.repeat([0, 255], 4), (64, 8)).astype(np.uint8)

    flat_metrics = complexity_metrics(flat)
    stripe_metrics = complexity_metrics(stripes)

    assert flat_metrics == {"sobel_si": 0.0}
    assert stripe_metrics["sobel_si"] > 0


def test_image_seed_is_stable_and_image_specific() -> None:
    assert derive_image_seed(20260811, 0) == derive_image_seed(20260811, 0)
    assert derive_image_seed(20260811, 0) != derive_image_seed(20260811, 1)


def test_generate_sweep_writes_unique_stable_pngs_and_manifest(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "stimuli"
    input_dir.mkdir()
    source = input_dir / "test image.png"
    pixels = np.tile(np.arange(64, dtype=np.uint8), (48, 1))
    assert cv2.imwrite(str(source), pixels)
    manifest = output_dir / "manifest.csv"

    first_rows = generate_sweep(input_dir, output_dir, "fixture", (5.0,), (7, 8), (10.0,), manifest)
    first_hashes = [row["sha256"] for row in first_rows]
    second_rows = generate_sweep(input_dir, output_dir, "fixture", (5.0,), (7, 8), (10.0,), manifest)

    assert len(first_rows) == 4
    assert len({row["path"] for row in first_rows}) == 4
    assert [row["sha256"] for row in second_rows] == first_hashes
    assert {row["distortion"] for row in first_rows} == {"clean", "awgn", "stripes"}
    assert {row["seed"] for row in first_rows if row["distortion"] == "awgn"} == {7, 8}
    assert all(Path(str(row["path"])).exists() for row in first_rows)
    assert all(
        hashlib.sha256(Path(str(row["path"])).read_bytes()).hexdigest() == row["sha256"]
        for row in first_rows
    )

    with manifest.open(encoding="utf-8", newline="") as stream:
        manifest_rows = list(csv.DictReader(stream))
    assert tuple(manifest_rows[0]) == MANIFEST_FIELDS
    assert len(manifest_rows) == 4
    assert manifest_rows[0]["dataset"] == "fixture"
    assert manifest_rows[0]["source"] == source.name
    assert manifest_rows[0]["width"] == "64"
    assert manifest_rows[0]["height"] == "48"
    awgn_rows = [row for row in manifest_rows if row["distortion"] == "awgn"]
    assert all(row["derived_seed"] for row in awgn_rows)
    assert all(float(row["actual_luma_rms"]) > 0 for row in awgn_rows)
