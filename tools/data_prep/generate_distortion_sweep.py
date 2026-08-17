from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from vvenc_csf.stimuli import (
    add_achromatic_awgn,
    add_sinusoidal_interference,
    complexity_metrics,
    derive_image_seed,
    file_sha256,
    luma_rms_difference,
    read_png,
    stable_stimulus_name,
    write_png,
)


METRIC_NAMES = ("sobel_si",)
MANIFEST_FIELDS = (
    "dataset",
    "source",
    "stimulus",
    "distortion",
    "level",
    "seed",
    "derived_seed",
    "actual_luma_rms",
    "path",
    "width",
    "height",
    "sha256",
    *(f"source_{name}" for name in METRIC_NAMES),
    *(f"stimulus_{name}" for name in METRIC_NAMES),
)


def parse_float_list(value: str) -> tuple[float, ...]:
    values = tuple(float(item.strip()) for item in value.split(",") if item.strip())
    if any(item <= 0 for item in values):
        raise argparse.ArgumentTypeError("levels must be positive; the clean condition represents level zero")
    return values


def parse_seed_list(value: str) -> tuple[int, ...]:
    values = tuple(int(item.strip()) for item in value.split(",") if item.strip())
    if not values:
        raise argparse.ArgumentTypeError("at least one seed is required")
    return values


def generate_sweep(
    input_dir: Path,
    output_dir: Path,
    dataset: str,
    sigmas: tuple[float, ...],
    seeds: tuple[int, ...],
    amplitudes: tuple[float, ...],
    manifest: Path,
) -> list[dict[str, object]]:
    sources = sorted(input_dir.glob("*.png"))
    if not sources:
        raise RuntimeError(f"No PNG images found: {input_dir}")

    rows: list[dict[str, object]] = []
    for source_index, source in enumerate(sources):
        image = read_png(source)
        source_sha256 = file_sha256(source)
        source_metrics = complexity_metrics(image)
        conditions = [("clean", 0.0, None, image)]
        conditions.extend(
            (
                "awgn",
                sigma,
                seed,
                add_achromatic_awgn(image, sigma, derive_image_seed(seed, source_index)),
            )
            for sigma in sigmas
            for seed in seeds
        )
        conditions.extend(
            ("stripes", amplitude, None, add_sinusoidal_interference(image, amplitude))
            for amplitude in amplitudes
        )

        for distortion, level, seed, stimulus_image in conditions:
            filename = stable_stimulus_name(source, source_sha256, distortion, level, seed)
            target = output_dir / filename
            write_png(target, stimulus_image)
            stimulus_metrics = complexity_metrics(stimulus_image)
            row: dict[str, object] = {
                "dataset": dataset,
                "source": source.name,
                "stimulus": target.stem,
                "distortion": distortion,
                "level": format(level, ".12g"),
                "seed": "" if seed is None else seed,
                "derived_seed": "" if seed is None else derive_image_seed(seed, source_index),
                "actual_luma_rms": luma_rms_difference(image, stimulus_image),
                "path": target.as_posix(),
                "width": image.shape[1],
                "height": image.shape[0],
                "sha256": file_sha256(target),
            }
            row.update((f"source_{name}", source_metrics[name]) for name in METRIC_NAMES)
            row.update((f"stimulus_{name}", stimulus_metrics[name]) for name in METRIC_NAMES)
            rows.append(row)

    manifest.parent.mkdir(parents=True, exist_ok=True)
    with manifest.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=MANIFEST_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate deterministic clean, AWGN, and sinusoidal PNG stimuli.")
    parser.add_argument("--input", type=Path, required=True, help="Directory containing source PNG images.")
    parser.add_argument("--output", type=Path, required=True, help="Output directory for generated PNG stimuli.")
    parser.add_argument("--dataset", required=True, help="Dataset label stored in the manifest.")
    parser.add_argument("--sigmas", type=parse_float_list, default=parse_float_list("5,15,30"))
    parser.add_argument("--seeds", type=parse_seed_list, default=parse_seed_list("20260811,20260812,20260813"))
    parser.add_argument("--amplitudes", type=parse_float_list, default=parse_float_list("8,16,32"))
    parser.add_argument("--manifest", type=Path, help="Manifest CSV path; defaults to <output>/manifest.csv.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    manifest = args.manifest or args.output / "manifest.csv"
    rows = generate_sweep(args.input, args.output, args.dataset, args.sigmas, args.seeds, args.amplitudes, manifest)
    print(f"Wrote {len(rows)} stimuli to {args.output}")
    print(f"Wrote {manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
