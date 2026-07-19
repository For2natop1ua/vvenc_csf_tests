from __future__ import annotations

import csv
from pathlib import Path

from tools.reporting.report_vtm_scaling_list_study import build_readme, per_image_summary


def test_scaling_list_readme_is_separate_study(tmp_path: Path) -> None:
    dataset = tmp_path / "standard_grayscale"
    dataset.mkdir()
    metrics_csv = dataset / "image_metrics.csv"
    with metrics_csv.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=[
                "dataset",
                "image",
                "qp",
                "mode",
                "bpp",
                "bitstream_bytes",
                "psnr_y",
                "msssim_luma",
                "psnr_hvs_m_luma",
                "haarpsi_luma",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "dataset": "standard_grayscale",
                "image": "baboon",
                "qp": 22,
                "mode": "scalinglist_default",
                "bpp": 1.2,
                "bitstream_bytes": 39000,
                "psnr_y": 32.0,
                "msssim_luma": 0.98,
                "psnr_hvs_m_luma": 28.0,
                "haarpsi_luma": 0.81,
            }
        )
    partition_dir = tmp_path / "partition_overlays" / "standard_grayscale"
    partition_dir.mkdir(parents=True)
    with (partition_dir / "summary.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=["dataset", "image", "mode", "qp", "width", "height", "cu_count", "min_area", "max_area", "avg_area", "dominant_sizes"],
        )
        writer.writeheader()
        writer.writerow(
            {
                "dataset": "standard_grayscale",
                "image": "baboon",
                "mode": "scalinglist_default",
                "qp": 22,
                "width": 512,
                "height": 512,
                "cu_count": 100,
                "min_area": 16,
                "max_area": 1024,
                "avg_area": 64.0,
                "dominant_sizes": "8x8:20",
            }
        )

    readme = build_readme(tmp_path, (("standard_grayscale", "Standard Grayscale", ("psnr_y", "msssim_luma", "psnr_hvs_m_luma", "haarpsi_luma")),), 32)

    assert "# VTM Scaling List Study" in readme
    assert "`--ScalingList=1`" in readme
    assert "baboon`, `goldhill`, and `peppers" in readme
    assert "### Baboon" in readme
    assert "Metric values by QP" in readme
    assert "CU partition statistics by QP" in readme
    assert "<details>" not in readme
    assert "behavior of the partitioning scheme as QP changes" in readme
    assert "rectangular blocks" in readme
    assert "Bitstream bytes (.vvc file size)" in readme
    assert "luma means the first Y plane" in readme
    assert "Metric provenance" in readme
    assert "ScalingList=0/off" in readme
    assert "CU color" not in readme
    assert "VTM 23.0 baseline and VTM 23.0 CSF" not in readme


def test_per_image_summary_uses_absolute_scaling_list_metrics() -> None:
    rows = [
        {"image": "baboon", "bpp": "1.0", "bitstream_bytes": "100", "psnr_y": "30", "msssim_luma": "0.90"},
        {"image": "baboon", "bpp": "2.0", "bitstream_bytes": "200", "psnr_y": "34", "msssim_luma": "0.98"},
    ]

    summary = per_image_summary(rows, ("psnr_y", "msssim_luma"))

    assert summary == [
        {
            "image": "baboon",
            "qp_points": 2,
            "bpp_mean": 1.5,
            "bitstream_bytes_mean": 150.0,
            "psnr_y_mean": 32.0,
            "msssim_luma_mean": 0.94,
        }
    ]
