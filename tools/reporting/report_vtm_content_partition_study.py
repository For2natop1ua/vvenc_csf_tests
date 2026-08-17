"""Build the publication artifacts for the VTM content-partition study."""

from __future__ import annotations

import argparse
import csv
import shutil
import sys
from collections import defaultdict
from pathlib import Path

import cv2
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import TwoSlopeNorm
from matplotlib.lines import Line2D

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.visualization.partition_overlay import render_partition_overlay


CLEAN_QPS = (22, 27, 32, 37)
DISTURBANCE_QPS = (22, 32, 37)
AWGN_LEVELS = (5, 15, 30)
STRIPE_LEVELS = (8, 16, 32)
AWGN_SEEDS = ("20260811", "20260812", "20260813")
REGISTERED_AWGN_SEED = AWGN_SEEDS[0]

CLEAN_TABLE_FIELDS = (
    "source",
    "source_sobel_si",
    "qp",
    "cu_count",
    "coded_area",
    "mean_area",
    "median_area",
    "mean_equivalent_side",
    "cu_density_per_mpixel",
    "dominant_sizes",
)
DISTURBANCE_TABLE_FIELDS = (
    "source",
    "distortion",
    "level",
    "seed",
    "actual_luma_rms",
    "qp",
    "cu_count",
    "min_area",
    "max_area",
    "mean_area",
    "median_area",
    "dominant_sizes",
)
REALIZATION_TABLE_FIELDS = (
    "source",
    "source_sobel_si",
    "sigma",
    "seed",
    "derived_seed",
    "actual_luma_rms",
    "qp",
    "cu_count",
    "mean_area",
    "median_area",
    "dominant_sizes",
)
VARIABILITY_TABLE_FIELDS = (
    "source",
    "sigma",
    "seed_count",
    "min_cu_count",
    "max_cu_count",
    "cu_count_range",
    "relative_cu_count_range_percent",
)


class ContentPartitionReporter:
    """Validate a consolidated result set and render its canonical report artifacts."""

    def __init__(self, results_root: Path, output: Path) -> None:
        self.results_root = results_root
        self.output = output
        self.summary_path = results_root / "partition_summary.csv"
        self.manifest_path = results_root / "manifest.csv"
        self.rows: list[dict[str, str]] = []
        self.manifest: dict[str, dict[str, str]] = {}

    @staticmethod
    def read_rows(path: Path) -> list[dict[str, str]]:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            return list(csv.DictReader(handle))

    @staticmethod
    def write_rows(
        path: Path,
        rows: list[dict[str, object]],
        fields: tuple[str, ...],
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)

    def load(self) -> None:
        self.rows = self.read_rows(self.summary_path)
        manifest_rows = self.read_rows(self.manifest_path)
        self.manifest = {row["stimulus"]: row for row in manifest_rows}
        if len(self.manifest) != len(manifest_rows):
            raise RuntimeError("The consolidated manifest contains duplicate stimulus names")
        if not self.rows:
            raise RuntimeError("The consolidated partition summary is empty")
        if any(row.get("mode") != "baseline" for row in self.rows):
            raise RuntimeError("The consolidated report accepts baseline VTM rows only")
        if any(row.get("reconstruction_verified", "").lower() != "true" for row in self.rows):
            raise RuntimeError("The consolidated summary contains an unverified reconstruction")
        if any(row.get("cu_coverage_verified", "").lower() != "true" for row in self.rows):
            raise RuntimeError("The consolidated summary contains unverified CU coverage")
        if any(row.get("stimulus") not in self.manifest for row in self.rows):
            raise RuntimeError("Every summary row must have a matching manifest entry")
        keys = {(row["stimulus"], int(row["qp"]), row["mode"]) for row in self.rows}
        if len(keys) != len(self.rows):
            raise RuntimeError("The consolidated summary contains duplicate encoding jobs")

    def stimulus_path(self, stimulus: str) -> Path:
        manifest_row = self.manifest.get(stimulus)
        if manifest_row is None:
            raise RuntimeError(f"Stimulus is absent from the manifest: {stimulus}")
        filename = Path(manifest_row.get("path", "")).name or f"{stimulus}.png"
        return self.results_root / "stimuli" / filename

    def partition_path(self, row: dict[str, str]) -> Path:
        stimulus = row["stimulus"]
        qp = int(row["qp"])
        return (
            self.results_root
            / "encoded"
            / stimulus
            / f"QP{qp}"
            / "baseline"
            / f"{stimulus}_QP{qp}_baseline.csv"
        )

    @staticmethod
    def condition_key(row: dict[str, str]) -> tuple[str, int, str]:
        return row["distortion"], int(float(row["level"])), row["seed"]

    @classmethod
    def condition_order(cls, row: dict[str, str]) -> tuple[int, int]:
        distortion, level, _ = cls.condition_key(row)
        return {"clean": 0, "awgn": 1, "stripes": 2}[distortion], level

    @classmethod
    def condition_name(cls, row: dict[str, str]) -> str:
        distortion, level, _ = cls.condition_key(row)
        if distortion == "clean":
            return "without_interference"
        if distortion == "awgn":
            return f"awgn_sigma_{level}"
        return f"stripes_amplitude_{level}"

    def clean_measurements(self) -> list[dict[str, str]]:
        selected = [
            row
            for row in self.rows
            if row.get("dataset") == "kodak"
            and row["distortion"] == "clean"
            and int(row["qp"]) in CLEAN_QPS
        ]
        by_source: dict[str, list[dict[str, str]]] = defaultdict(list)
        for row in selected:
            by_source[row["source"]].append(row)
        if len(selected) != 96 or len(by_source) != 24:
            raise RuntimeError("Expected the complete 24-source by 4-QP clean Kodak matrix")
        for source, rows in by_source.items():
            if sorted(int(row["qp"]) for row in rows) != list(CLEAN_QPS):
                raise RuntimeError(f"Incomplete clean QP matrix for {source}")
        return sorted(selected, key=lambda row: (row["source"], int(row["qp"])))

    def disturbance_measurements(self) -> list[dict[str, str]]:
        expected_conditions = {
            ("clean", 0, ""),
            *(("awgn", level, REGISTERED_AWGN_SEED) for level in AWGN_LEVELS),
            *(("stripes", level, "") for level in STRIPE_LEVELS),
        }
        selected = [
            row
            for row in self.rows
            if row.get("dataset") == "kodak"
            and int(row["qp"]) in DISTURBANCE_QPS
            and self.condition_key(row) in expected_conditions
        ]
        sources = {row["source"] for row in selected}
        keys = {
            (row["source"], int(row["qp"]), *self.condition_key(row))
            for row in selected
        }
        expected_count = 24 * len(DISTURBANCE_QPS) * len(expected_conditions)
        if len(sources) != 24 or len(selected) != expected_count or len(keys) != expected_count:
            raise RuntimeError("Expected the complete 24-source disturbance-by-QP matrix")
        return sorted(
            selected,
            key=lambda row: (row["source"], int(row["qp"]), self.condition_order(row)),
        )

    def realization_measurements(self) -> list[dict[str, str]]:
        selected = [
            row
            for row in self.rows
            if row.get("dataset") == "kodak"
            and row["distortion"] == "awgn"
            and int(row["qp"]) == 32
            and int(float(row["level"])) in AWGN_LEVELS
            and row["seed"] in AWGN_SEEDS
        ]
        sources = {row["source"] for row in selected}
        keys = {
            (row["source"], int(float(row["level"])), row["seed"])
            for row in selected
        }
        expected_count = 24 * len(AWGN_LEVELS) * len(AWGN_SEEDS)
        if len(sources) != 24 or len(selected) != expected_count or len(keys) != expected_count:
            raise RuntimeError("Expected the complete 24-source AWGN-realization matrix")
        return sorted(
            selected,
            key=lambda row: (row["source"], int(float(row["level"])), row["seed"]),
        )

    @staticmethod
    def extreme_si_sources(rows: list[dict[str, str]]) -> tuple[str, str]:
        source_si: dict[str, float] = {}
        for row in rows:
            value = float(row["source_sobel_si"])
            previous = source_si.setdefault(row["source"], value)
            if previous != value:
                raise RuntimeError(f"Inconsistent source SI for {row['source']}")
        ordered = sorted(source_si, key=lambda source: (source_si[source], source))
        return ordered[0], ordered[-1]

    @staticmethod
    def representative_source(rows: list[dict[str, str]]) -> str:
        source_si = {
            row["source"]: float(row["source_sobel_si"])
            for row in rows
            if row["distortion"] == "clean"
        }
        ordered = sorted(source_si, key=lambda source: (source_si[source], source))
        if len(ordered) != 24:
            raise RuntimeError("Representative-source selection requires all 24 Kodak images")
        return ordered[len(ordered) // 2]

    @staticmethod
    def realization_variability(
        rows: list[dict[str, str]],
    ) -> list[dict[str, object]]:
        cells: list[dict[str, object]] = []
        for source in sorted({row["source"] for row in rows}):
            for sigma in AWGN_LEVELS:
                values = [
                    int(row["cu_count"])
                    for row in rows
                    if row["source"] == source and int(float(row["level"])) == sigma
                ]
                if len(values) != len(AWGN_SEEDS):
                    raise RuntimeError(f"Incomplete realization cell for {source}, sigma {sigma}")
                count_range = max(values) - min(values)
                cells.append(
                    {
                        "source": source,
                        "sigma": sigma,
                        "seed_count": len(values),
                        "min_cu_count": min(values),
                        "max_cu_count": max(values),
                        "cu_count_range": count_range,
                        "relative_cu_count_range_percent": (
                            100.0 * count_range / (sum(values) / len(values))
                        ),
                    }
                )
        return cells

    @staticmethod
    def max_variability_example(cells: list[dict[str, object]]) -> tuple[str, int]:
        selected = max(
            cells,
            key=lambda row: (
                float(row["relative_cu_count_range_percent"]),
                str(row["source"]),
                int(row["sigma"]),
            ),
        )
        return str(selected["source"]), int(selected["sigma"])

    @staticmethod
    def paired_area_changes(
        rows: list[dict[str, str]],
        distortion: str,
        level: int,
        qp: int,
    ) -> dict[str, float]:
        clean = {
            row["source"]: float(row["mean_area"])
            for row in rows
            if int(row["qp"]) == qp and row["distortion"] == "clean"
        }
        changed = {
            row["source"]: float(row["mean_area"])
            for row in rows
            if int(row["qp"]) == qp
            and row["distortion"] == distortion
            and int(float(row["level"])) == level
        }
        if clean.keys() != changed.keys() or len(clean) != 24:
            raise RuntimeError(f"Incomplete paired response for {distortion}, QP {qp}, level {level}")
        return {
            source: (changed[source] / clean[source] - 1.0) * 100.0
            for source in sorted(clean)
        }

    @staticmethod
    def save_figure(figure: plt.Figure, output: Path) -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        figure.savefig(output, dpi=240, bbox_inches="tight")
        plt.close(figure)

    def render_overlay(self, row: dict[str, str], output: Path) -> None:
        image_path = self.stimulus_path(row["stimulus"])
        image = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
        if image is None:
            raise RuntimeError(f"Could not read stimulus: {image_path}")
        blocks_path = self.partition_path(row)
        blocks = self.read_rows(blocks_path)
        if len(blocks) != int(row["cu_count"]):
            raise RuntimeError(f"CU count differs from summary for {blocks_path}")
        height, width = image.shape[:2]
        render_partition_overlay(
            blocks,
            width,
            height,
            output,
            image_path,
            cu_color=(255, 87, 0),
        )

    def build_clean_artifacts(self, rows: list[dict[str, str]]) -> None:
        self.write_rows(
            self.output / "tables" / "clean_cu_measurements.csv",
            rows,
            CLEAN_TABLE_FIELDS,
        )
        lowest, highest = self.extreme_si_sources(rows)
        for source in (lowest, highest):
            source_rows = [row for row in rows if row["source"] == source]
            clean_image = self.stimulus_path(source_rows[0]["stimulus"])
            destination = self.output / "examples" / "sources" / source
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(clean_image, destination)
            for row in source_rows:
                self.render_overlay(
                    row,
                    self.output
                    / "examples"
                    / "partition_maps"
                    / "complexity"
                    / Path(source).stem
                    / f"QP{row['qp']}.png",
                )

    def build_disturbance_examples(
        self,
        rows: list[dict[str, str]],
        source: str,
    ) -> None:
        selected = [row for row in rows if row["source"] == source]
        for row in selected:
            distortion, level, _ = self.condition_key(row)
            if int(row["qp"]) == 32:
                source_path = self.stimulus_path(row["stimulus"])
                destination = (
                    self.output
                    / "examples"
                    / "stimuli"
                    / "interference"
                    / Path(source).stem
                    / f"{self.condition_name(row)}.png"
                )
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_path, destination)
            if (
                distortion == "clean"
                or (distortion == "awgn" and level == max(AWGN_LEVELS))
                or (distortion == "stripes" and level == max(STRIPE_LEVELS))
            ):
                self.render_overlay(
                    row,
                    self.output
                    / "examples"
                    / "partition_maps"
                    / "interference"
                    / Path(source).stem
                    / f"QP{row['qp']}"
                    / f"{self.condition_name(row)}.png",
                )

    def plot_disturbance_responses(
        self,
        rows: list[dict[str, str]],
        output: Path,
    ) -> None:
        specifications = (
            ("awgn", "AWGN", "Nominal standard deviation σ, 8-bit code values", AWGN_LEVELS),
            (
                "stripes",
                "Sinusoidal interference",
                "Nominal peak amplitude A, 8-bit code values",
                STRIPE_LEVELS,
            ),
        )
        sources = sorted({row["source"] for row in rows})
        colors = [*plt.get_cmap("tab20").colors, *plt.get_cmap("Dark2").colors[:4]]
        line_styles = ("-", "--", "-.", ":")
        markers = ("o", "s", "^", "D", "v", "P")
        styles = {
            source: (colors[index], line_styles[index % 4], markers[index % 6])
            for index, source in enumerate(sources)
        }
        with plt.rc_context({"font.size": 9, "axes.titlesize": 10, "axes.labelsize": 9}):
            figure, axes = plt.subplots(2, 3, figsize=(12, 8), sharey=True)
            for row_index, (distortion, title, xlabel, levels) in enumerate(specifications):
                for column_index, qp in enumerate(DISTURBANCE_QPS):
                    axis = axes[row_index, column_index]
                    changes = [self.paired_area_changes(rows, distortion, level, qp) for level in levels]
                    for source in sources:
                        color, line_style, marker = styles[source]
                        axis.plot(
                            levels,
                            [condition[source] for condition in changes],
                            color=color,
                            linestyle=line_style,
                            marker=marker,
                            markersize=2.7,
                            linewidth=0.9,
                            alpha=0.88,
                        )
                    axis.axhline(0.0, color="#4B5563", linewidth=0.8)
                    axis.set_title(f"{title} · QP {qp}")
                    axis.set_xlabel(xlabel)
                    axis.set_xticks(levels)
                    axis.grid(axis="y", color="#D1D5DB", linewidth=0.6, alpha=0.7)
                    axis.spines[["top", "right"]].set_visible(False)
                    axis.text(
                        0.01,
                        0.97,
                        f"({chr(97 + row_index * 3 + column_index)})",
                        transform=axis.transAxes,
                        ha="left",
                        va="top",
                        fontweight="bold",
                    )
                axes[row_index, 0].set_ylabel("Change in mean CU area\nrelative to control, %")
            axes[0, 0].set_ylim(-60, 100)
            handles = [
                Line2D(
                    [0],
                    [0],
                    color=styles[source][0],
                    linestyle=styles[source][1],
                    marker=styles[source][2],
                    markersize=3,
                    linewidth=1,
                    label=Path(source).stem,
                )
                for source in sources
            ]
            figure.legend(
                handles=handles,
                loc="lower center",
                bbox_to_anchor=(0.5, 0.025),
                ncol=8,
                frameon=False,
                fontsize=8,
                handlelength=2.4,
                columnspacing=1.15,
                handletextpad=0.5,
            )
            figure.tight_layout(rect=(0, 0.095, 1, 1), h_pad=1.5, w_pad=1.2)
            self.save_figure(figure, output)

    def plot_disturbance_heatmap(
        self,
        rows: list[dict[str, str]],
        output: Path,
    ) -> None:
        source_si = {
            row["source"]: float(row["source_sobel_si"])
            for row in rows
            if row["distortion"] == "clean"
        }
        sources = sorted(source_si, key=lambda source: (source_si[source], source))
        specifications = (
            ("awgn", "AWGN", "σ", AWGN_LEVELS),
            ("stripes", "Sinusoidal interference", "A", STRIPE_LEVELS),
        )
        matrices = []
        for distortion, _, _, levels in specifications:
            columns = [
                self.paired_area_changes(rows, distortion, level, qp)
                for qp in DISTURBANCE_QPS
                for level in levels
            ]
            matrices.append(
                np.asarray([[column[source] for column in columns] for source in sources])
            )
        limit = float(
            np.ceil(max(np.max(np.abs(matrix)) for matrix in matrices) / 10.0) * 10.0
        )
        norm = TwoSlopeNorm(vmin=-limit, vcenter=0.0, vmax=limit)
        with plt.rc_context({"font.size": 8, "axes.titlesize": 10, "axes.labelsize": 9}):
            figure, axes = plt.subplots(
                1,
                2,
                figsize=(12, 7.4),
                sharey=True,
                layout="constrained",
            )
            image = None
            for axis, matrix, (_, title, symbol, levels) in zip(
                axes, matrices, specifications, strict=True
            ):
                image = axis.imshow(matrix, aspect="auto", cmap="RdBu_r", norm=norm)
                axis.set_title(title)
                axis.set_xticks(
                    range(9),
                    [
                        f"QP {qp}\n{symbol}={level}"
                        for qp in DISTURBANCE_QPS
                        for level in levels
                    ],
                )
                axis.set_yticks(range(len(sources)), [Path(source).stem for source in sources])
                axis.tick_params(axis="x", labelrotation=45)
                axis.tick_params(length=0)
                axis.axvline(2.5, color="white", linewidth=1.5)
                axis.axvline(5.5, color="white", linewidth=1.5)
            axes[0].set_ylabel("Kodak images ordered by SI")
            if image is None:
                raise RuntimeError("Heatmap image was not created")
            colorbar = figure.colorbar(image, ax=axes, location="bottom", shrink=0.75, pad=0.08)
            colorbar.set_label("Change in mean CU area relative to control, %")
            self.save_figure(figure, output)

    def build_disturbance_artifacts(self, rows: list[dict[str, str]]) -> None:
        self.write_rows(
            self.output / "tables" / "interference_cu_measurements.csv",
            rows,
            DISTURBANCE_TABLE_FIELDS,
        )
        self.build_disturbance_examples(rows, self.representative_source(rows))
        self.plot_disturbance_responses(
            rows,
            self.output / "figures" / "interference_cu_area_change.png",
        )
        self.plot_disturbance_heatmap(
            rows,
            self.output / "figures" / "interference_cu_area_heatmap.png",
        )

    def plot_realization_variability(
        self,
        cells: list[dict[str, object]],
        output: Path,
    ) -> None:
        sources = sorted({str(row["source"]) for row in cells})
        colors = [*plt.get_cmap("tab20").colors, *plt.get_cmap("Dark2").colors[:4]]
        line_styles = ("-", "--", "-.", ":")
        markers = ("o", "s", "^", "D", "v", "P")
        styles = {
            source: (colors[index], line_styles[index % 4], markers[index % 6])
            for index, source in enumerate(sources)
        }
        with plt.rc_context({"font.size": 10, "axes.titlesize": 12, "axes.labelsize": 10}):
            figure, axis = plt.subplots(figsize=(10, 6.4))
            for source in sources:
                values = {
                    int(row["sigma"]): float(row["relative_cu_count_range_percent"])
                    for row in cells
                    if row["source"] == source
                }
                color, line_style, marker = styles[source]
                axis.plot(
                    AWGN_LEVELS,
                    [values[sigma] for sigma in AWGN_LEVELS],
                    color=color,
                    linestyle=line_style,
                    marker=marker,
                    markersize=5,
                    linewidth=1.2,
                )
            axis.set_title("Across-realization CU-count variability · AWGN, QP 32")
            axis.set_xlabel("Nominal standard deviation σ, 8-bit code values")
            axis.set_ylabel("Mean-normalized across-realization range of CU count, %")
            axis.set_xticks(AWGN_LEVELS)
            axis.grid(axis="y", color="#D1D5DB", linewidth=0.6, alpha=0.8)
            axis.spines[["top", "right"]].set_visible(False)
            handles = [
                Line2D(
                    [0],
                    [0],
                    color=styles[source][0],
                    linestyle=styles[source][1],
                    marker=styles[source][2],
                    markersize=4,
                    linewidth=1.1,
                    label=Path(source).stem,
                )
                for source in sources
            ]
            figure.legend(
                handles=handles,
                loc="lower center",
                bbox_to_anchor=(0.5, 0.02),
                ncol=8,
                frameon=False,
                fontsize=8.5,
                handlelength=2.4,
                columnspacing=1.2,
            )
            figure.tight_layout(rect=(0, 0.13, 1, 1))
            self.save_figure(figure, output)

    def build_realization_artifacts(
        self,
        rows: list[dict[str, str]],
        cells: list[dict[str, object]],
    ) -> None:
        full_rows = [{"sigma": row["level"], **row} for row in rows]
        self.write_rows(
            self.output / "tables" / "awgn_realization_cu_measurements.csv",
            full_rows,
            REALIZATION_TABLE_FIELDS,
        )
        self.write_rows(
            self.output / "tables" / "awgn_realization_variability.csv",
            cells,
            VARIABILITY_TABLE_FIELDS,
        )
        source, sigma = self.max_variability_example(cells)
        example_rows = [
            row
            for row in rows
            if row["source"] == source and int(float(row["level"])) == sigma
        ]
        for row in example_rows:
            label = f"sigma_{sigma}_seed_{row['seed']}"
            source_path = self.stimulus_path(row["stimulus"])
            stimulus_output = (
                self.output
                / "examples"
                / "stimuli"
                / "noise_realizations"
                / Path(source).stem
                / f"{label}.png"
            )
            stimulus_output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, stimulus_output)
            self.render_overlay(
                row,
                self.output
                / "examples"
                / "partition_maps"
                / "noise_realizations"
                / Path(source).stem
                / "QP32"
                / f"{label}.png",
            )
        self.plot_realization_variability(
            cells,
            self.output / "figures" / "awgn_realization_cu_count_variability.png",
        )

    def build(self) -> None:
        self.load()
        clean = self.clean_measurements()
        disturbances = self.disturbance_measurements()
        realizations = self.realization_measurements()
        variability = self.realization_variability(realizations)
        self.build_clean_artifacts(clean)
        self.build_disturbance_artifacts(disturbances)
        self.build_realization_artifacts(realizations, variability)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--results-root",
        type=Path,
        default=Path("results/vtm_content_partition_study"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("docs/vtm_content_partition_study"),
    )
    args = parser.parse_args()

    reporter = ContentPartitionReporter(args.results_root, args.output)
    reporter.build()
    print(f"Wrote VTM content-partition artifacts to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
