from __future__ import annotations

import argparse
import csv
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.reporting.report_vtm_scaling_list_study import main as render_report
from tools.visualization.parse_vvenc_qp_trace import parse_trace, write_csv as write_partition_csv
from tools.visualization.partition_overlay import render_partition_overlay
from vvenc_csf.benchmark import EncoderLogParser, VisualMetricCalculator
from vvenc_csf.core import CommandRunner, ffprobe_size, files_equal, parse_qps, platform_executable, write_csv
from vvenc_csf.encoding import DecoderRunner, EncodeJob, EncoderRunner, ImageConverter, VTM_ENCODER_CONFIG


DEFAULT_ENCODER = platform_executable(Path("binaries/vtm/vtm23/baseline/EncoderApp"))
DEFAULT_DECODER = platform_executable(Path("binaries/vtm/vtm23/baseline/DecoderApp"))
DEFAULT_TRACE_ENCODER = platform_executable(Path("binaries/vtm/vtm23/baseline_trace/EncoderApp"))
DEFAULT_IMAGES = ("baboon", "goldhill", "peppers")
OVERLAY_CU_COLOR_BGR = (255, 87, 0)  # RGB #0057FF
SCALING_LIST_COMPARISON_MODES = (
    ("scalinglist_off", ("--ScalingList=0",)),
    ("scalinglist_default", ("--ScalingList=1",)),
)


def selected_images(directory: Path, names: tuple[str, ...]) -> list[Path]:
    images = {path.stem.lower(): path for path in directory.glob("*.png")}
    missing = [name for name in names if name not in images]
    if missing:
        raise RuntimeError(f"Missing PNG images in {directory}: {', '.join(missing)}")
    return [images[name] for name in names]


def convert_bmp_dir_to_png(source: Path, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    for bmp in sorted(source.glob("*.bmp")):
        stem = "peppers" if bmp.stem.lower() == "pepper" else bmp.stem
        target = output / f"{stem}.png"
        if target.exists():
            continue
        import cv2

        image = cv2.imread(str(bmp), cv2.IMREAD_UNCHANGED)
        if image is None:
            raise RuntimeError(f"Could not read BMP image: {bmp}")
        if not cv2.imwrite(str(target), image, [cv2.IMWRITE_PNG_COMPRESSION, 3]):
            raise RuntimeError(f"Could not write PNG image: {target}")


def ensure_dataset(png_dir: Path, bmp_dir: Path | None = None) -> Path:
    if not list(png_dir.glob("*.png")) and bmp_dir and bmp_dir.exists():
        convert_bmp_dir_to_png(bmp_dir, png_dir)
    if not list(png_dir.glob("*.png")):
        raise RuntimeError(f"No PNG images found: {png_dir}")
    return png_dir


def run_metrics(args: argparse.Namespace, dataset: str, images: list[Path]) -> None:
    runner = CommandRunner()
    converter = ImageConverter(runner)
    encoder = EncoderRunner(runner)
    decoder = DecoderRunner(args.decoder, runner)
    metrics = VisualMetricCalculator(runner)
    parser = EncoderLogParser()
    rows: list[dict[str, object]] = []

    for image in images:
        width, height = ffprobe_size(image, runner)
        yuv = args.results / dataset / "yuv" / f"{image.stem}_{width}x{height}_1.yuv"
        if args.conversion == "opencv_444":
            converter.to_yuv444p_opencv(image, yuv)
        else:
            converter.to_yuv444p(image, yuv)
        raw_bytes = width * height * 3

        for qp in parse_qps(args.qps):
            out_dir = args.results / dataset / "encoded" / image.stem / f"QP{qp}" / "scalinglist_default"
            bitstream = out_dir / f"{image.stem}_QP{qp}_scalinglist_default.vvc"
            recon = out_dir / f"{image.stem}_QP{qp}_scalinglist_default_rec.yuv"
            decoded = out_dir / f"{image.stem}_QP{qp}_scalinglist_default_dec.yuv"
            text = encoder.encode(
                EncodeJob(
                    encoder=args.encoder,
                    yuv=yuv,
                    width=width,
                    height=height,
                    qp=qp,
                    preset=args.preset,
                    bitstream=bitstream,
                    recon=recon,
                    log=out_dir / f"{image.stem}_QP{qp}_scalinglist_default_enc.log",
                    extra_args=("--ScalingList=1",),
                    codec="vtm",
                )
            )
            decoder.decode(bitstream, decoded, out_dir / f"{image.stem}_QP{qp}_scalinglist_default_dec.log")
            if not files_equal(recon, decoded):
                raise RuntimeError(f"Decoded YUV differs from encoder reconstruction: {decoded}")
            bytes_out = bitstream.stat().st_size
            rows.append(
                {
                    "dataset": dataset,
                    "image": image.stem,
                    "width": width,
                    "height": height,
                    "qp": qp,
                    "mode": "scalinglist_default",
                    "bitstream_bytes": bytes_out,
                    "compression_ratio": raw_bytes / bytes_out if bytes_out else 0,
                    "bpp": (bytes_out * 8) / (width * height),
                    **parser.parse(text),
                    **metrics.calculate(yuv, recon, width, height, chroma_format="444"),
                }
            )

    write_csv(args.results / dataset / "image_metrics.csv", rows)


def run_scaling_list_comparison(args: argparse.Namespace, dataset: str, images: list[Path]) -> None:
    """Run paired VTM ScalingList=0 and ScalingList=1 encodes under identical inputs."""

    runner = CommandRunner()
    converter = ImageConverter(runner)
    encoder = EncoderRunner(runner)
    decoder = DecoderRunner(args.decoder, runner)
    metrics = VisualMetricCalculator(runner)
    parser = EncoderLogParser()
    metric_rows: list[dict[str, object]] = []

    for image in images:
        width, height = ffprobe_size(image, runner)
        yuv = args.results / "scaling_list_comparison" / dataset / "yuv" / f"{image.stem}_{width}x{height}_1.yuv"
        if args.conversion == "opencv_444":
            converter.to_yuv444p_opencv(image, yuv)
        else:
            converter.to_yuv444p(image, yuv)
        raw_bytes = width * height * 3

        for qp in parse_qps(args.qps):
            for mode, extra_args in SCALING_LIST_COMPARISON_MODES:
                out_dir = args.results / "scaling_list_comparison" / dataset / "encoded" / image.stem / f"QP{qp}" / mode
                bitstream = out_dir / f"{image.stem}_QP{qp}_{mode}.vvc"
                recon = out_dir / f"{image.stem}_QP{qp}_{mode}_rec.yuv"
                decoded = out_dir / f"{image.stem}_QP{qp}_{mode}_dec.yuv"
                encode_log = out_dir / f"{image.stem}_QP{qp}_{mode}_enc.log"
                if args.reuse_comparison_encodes and bitstream.exists() and recon.exists() and encode_log.exists():
                    text = encode_log.read_text(encoding="utf-8", errors="replace")
                else:
                    text = encoder.encode(
                        EncodeJob(
                            encoder=args.encoder,
                            yuv=yuv,
                            width=width,
                            height=height,
                            qp=qp,
                            preset=args.preset,
                            bitstream=bitstream,
                            recon=recon,
                            log=encode_log,
                            extra_args=extra_args,
                            codec="vtm",
                        )
                    )
                if not (args.reuse_comparison_encodes and decoded.exists()):
                    decoder.decode(bitstream, decoded, out_dir / f"{image.stem}_QP{qp}_{mode}_dec.log")
                if not files_equal(recon, decoded):
                    raise RuntimeError(f"Decoded YUV differs from encoder reconstruction: {decoded}")
                bytes_out = bitstream.stat().st_size
                metric_rows.append(
                    {
                        "dataset": dataset,
                        "image": image.stem,
                        "width": width,
                        "height": height,
                        "qp": qp,
                        "mode": mode,
                        "scaling_list": "0" if mode == "scalinglist_off" else "1",
                        "bitstream_bytes": bytes_out,
                        "compression_ratio": raw_bytes / bytes_out if bytes_out else 0,
                        "bpp": (bytes_out * 8) / (width * height),
                        **parser.parse(text),
                        **metrics.calculate(yuv, recon, width, height, chroma_format="444"),
                    }
                )

    comparison_dir = args.results / "scaling_list_comparison" / dataset
    write_csv(comparison_dir / "scaling_list_mode_metrics.csv", metric_rows)
    write_csv(comparison_dir / "scaling_list_mode_comparison.csv", scaling_list_comparison_rows(metric_rows))


def scaling_list_comparison_rows(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    by_key: dict[tuple[str, int], dict[str, dict[str, object]]] = {}
    metrics = ("bpp", "bitstream_bytes", "psnr_y", "psnr_rgb", "msssim_luma", "msssim_rgb", "psnr_hvs_m_luma", "haarpsi_luma")
    for row in rows:
        by_key.setdefault((str(row["image"]), int(row["qp"])), {})[str(row["mode"])] = row

    output: list[dict[str, object]] = []
    for (image, qp), modes in sorted(by_key.items()):
        sl0 = modes.get("scalinglist_off")
        sl1 = modes.get("scalinglist_default")
        if sl0 is None or sl1 is None:
            continue
        item: dict[str, object] = {
            "image": image,
            "qp": qp,
            "scaling_list_0_mode": "paired --ScalingList=0",
            "scaling_list_1_mode": "paired --ScalingList=1",
        }
        for metric in metrics:
            if metric not in sl0 or metric not in sl1:
                continue
            sl0_value = float(sl0[metric])
            sl1_value = float(sl1[metric])
            item[f"{metric}_sl0"] = sl0_value
            item[f"{metric}_sl1"] = sl1_value
            item[f"{metric}_delta"] = sl1_value - sl0_value
            if metric in {"bpp", "bitstream_bytes"}:
                item[f"{metric}_delta_pct"] = _pct(sl1_value, sl0_value)
        output.append(item)
    return output


def build_partition_overlays(args: argparse.Namespace, dataset: str, images: list[Path]) -> None:
    runner = CommandRunner()
    converter = ImageConverter(runner)
    rows_out: list[dict[str, object]] = []

    for image in images:
        width, height = ffprobe_size(image, runner)
        yuv = args.results / "partition_work" / dataset / "yuv" / f"{image.stem}_{width}x{height}_1.yuv"
        yuv_ready = False

        for qp in parse_qps(args.qps):
            run_dir = args.results / "partition_work" / dataset / image.stem / f"QP{qp}" / "scalinglist_default"
            bitstream = run_dir / f"{image.stem}_QP{qp}_scalinglist_default.vvc"
            recon = run_dir / f"{image.stem}_QP{qp}_scalinglist_default_rec.yuv"
            trace = run_dir / f"{image.stem}_QP{qp}_scalinglist_default_d_qp.trace"
            log = run_dir / f"{image.stem}_QP{qp}_scalinglist_default.log"
            csv_path = run_dir / f"{image.stem}_QP{qp}_scalinglist_default.csv"
            run_dir.mkdir(parents=True, exist_ok=True)
            if args.reuse_partition_csv and csv_path.exists():
                with csv_path.open("r", encoding="utf-8-sig", newline="") as stream:
                    rows = list(csv.DictReader(stream))
            else:
                if not yuv_ready:
                    if args.conversion == "opencv_444":
                        converter.to_yuv444p_opencv(image, yuv)
                    else:
                        converter.to_yuv444p(image, yuv)
                    yuv_ready = True
                runner.run(
                    [
                        str(args.trace_encoder),
                        "-c",
                        str(VTM_ENCODER_CONFIG),
                        "-i",
                        str(yuv),
                        "-wdt",
                        str(width),
                        "-hgt",
                        str(height),
                        "-fr",
                        "1",
                        "-f",
                        "1",
                        "-q",
                        str(qp),
                        "-b",
                        str(bitstream),
                        "-o",
                        str(recon),
                        "--InputChromaFormat=444",
                        "--InputBitDepth=8",
                        "--ScalingList=1",
                        f"--TraceFile={trace}",
                        "--TraceRule=D_QP:poc==0",
                    ],
                    log,
                )
                rows = parse_trace(trace, frame=0, mode="scalinglist_default")
            png_path = args.output / "partition_overlays" / dataset / f"QP{qp}" / f"{image.stem}_scalinglist_default.png"
            write_partition_csv(rows, csv_path)
            render_partition_overlay(
                [{key: str(value) for key, value in row.items()} for row in rows],
                width,
                height,
                png_path,
                image,
                cu_color=OVERLAY_CU_COLOR_BGR,
            )
            rows_out.append(summarize(rows, dataset, image.stem, qp, width, height))

    write_partition_summary(args.output / "partition_overlays" / dataset / "summary.csv", rows_out)


def summarize(rows: list[dict[str, int | str]], dataset: str, image: str, qp: int, width: int, height: int) -> dict[str, object]:
    areas = [int(row["width"]) * int(row["height"]) for row in rows]
    sizes: dict[str, int] = {}
    for row in rows:
        key = f'{row["width"]}x{row["height"]}'
        sizes[key] = sizes.get(key, 0) + 1
    return {
        "dataset": dataset,
        "image": image,
        "mode": "scalinglist_default",
        "qp": qp,
        "width": width,
        "height": height,
        "cu_count": len(rows),
        "min_area": min(areas),
        "max_area": max(areas),
        "avg_area": sum(areas) / len(areas),
        "dominant_sizes": "; ".join(f"{key}:{value}" for key, value in sorted(sizes.items(), key=lambda item: (-item[1], item[0]))[:6]),
    }


def write_partition_summary(output: Path, rows: list[dict[str, object]]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        return
    with output.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _pct(new_value: float, base_value: float) -> float:
    if base_value == 0:
        return 0.0
    return ((new_value - base_value) / base_value) * 100.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run VTM --ScalingList=1 QP study for baboon, goldhill, and peppers.")
    parser.add_argument("--results", type=Path, default=Path("results/vtm_scaling_list_study"))
    parser.add_argument("--output", type=Path, default=Path("docs/vtm_scaling_list_study"))
    parser.add_argument("--standard-grayscale-dir", type=Path, default=Path("data/datasets/images/standard_grayscale/png"))
    parser.add_argument("--standard-color-dir", type=Path, default=Path("data/datasets/images/standard_color/png"))
    parser.add_argument("--standard-color-bmp-dir", type=Path, default=Path("data/datasets/images/standart_color/bmp"))
    parser.add_argument("--images", default=",".join(DEFAULT_IMAGES))
    parser.add_argument("--qps", default="22,27,32,37")
    parser.add_argument("--preset", default="medium")
    parser.add_argument("--conversion", choices=("opencv_444", "ffmpeg_444"), default="opencv_444")
    parser.add_argument("--encoder", type=Path, default=DEFAULT_ENCODER)
    parser.add_argument("--decoder", type=Path, default=DEFAULT_DECODER)
    parser.add_argument("--trace-encoder", type=Path, default=DEFAULT_TRACE_ENCODER)
    parser.add_argument("--skip-metrics", action="store_true")
    parser.add_argument("--skip-partitions", action="store_true")
    parser.add_argument("--skip-comparison", action="store_true")
    parser.add_argument("--comparison-only", action="store_true")
    parser.add_argument("--reuse-comparison-encodes", action="store_true", help="Reuse existing paired ScalingList=0/1 bitstreams and reconstructions.")
    parser.add_argument("--reuse-partition-csv", action="store_true", help="Re-render partition overlays from existing trace CSV files when available.")
    parser.add_argument("--clean", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.clean:
        for path in (args.results, args.output):
            if path.exists():
                shutil.rmtree(path)
    args.results.mkdir(parents=True, exist_ok=True)
    args.output.mkdir(parents=True, exist_ok=True)

    names = tuple(name.strip().lower() for name in args.images.split(",") if name.strip())
    datasets = [
        ("standard_grayscale", ensure_dataset(args.standard_grayscale_dir)),
        ("standard_color", ensure_dataset(args.standard_color_dir, args.standard_color_bmp_dir)),
    ]
    for dataset, png_dir in datasets:
        images = selected_images(png_dir, names)
        if not args.comparison_only and not args.skip_metrics:
            run_metrics(args, dataset, images)
        if not args.comparison_only and not args.skip_partitions:
            build_partition_overlays(args, dataset, images)
        if not args.skip_comparison:
            run_scaling_list_comparison(args, dataset, images)

    old_argv = sys.argv
    try:
        sys.argv = ["report_vtm_scaling_list_study.py", "--results", str(args.results), "--output", str(args.output)]
        render_report()
    finally:
        sys.argv = old_argv
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
