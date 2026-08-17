from __future__ import annotations

from pathlib import Path

from vvenc_csf.core import CommandResult, platform_executable
from vvenc_csf.encoding import DecoderRunner, EncodeJob, EncoderRunner


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], Path | None]] = []

    def run(self, cmd: list[str], log_file: Path | None = None) -> CommandResult:
        self.calls.append((cmd, log_file))
        return CommandResult(cmd, "ok", 0)


def test_encoder_runner_builds_vvenc_command(tmp_path: Path) -> None:
    runner = RecordingRunner()
    encoder = platform_executable(Path("vvenc"))
    result = EncoderRunner(runner).encode(
        EncodeJob(
            encoder=encoder,
            yuv=Path("input.yuv"),
            width=512,
            height=512,
            qp=32,
            preset="medium",
            bitstream=tmp_path / "out.vvc",
            recon=tmp_path / "out.yuv",
            log=tmp_path / "encode.log",
            extra_args=("--CSFScalingList", "1"),
        )
    )

    cmd, log_file = runner.calls[0]
    assert result == "ok"
    assert cmd[:2] == [str(encoder), "--InputFile"]
    assert "--QP" in cmd and "32" in cmd
    assert "--CSFScalingList" in cmd and "1" in cmd
    assert log_file == tmp_path / "encode.log"


def test_encoder_runner_builds_vtm_command(tmp_path: Path) -> None:
    runner = RecordingRunner()
    encoder = Path("EncoderApp_csf.exe")
    EncoderRunner(runner).encode(
        EncodeJob(
            encoder=encoder,
            yuv=Path("input.yuv"),
            width=512,
            height=512,
            qp=32,
            preset="medium",
            bitstream=tmp_path / "out.vvc",
            recon=tmp_path / "out.yuv",
            log=tmp_path / "encode.log",
            extra_args=("--CSFScalingList=1", "--TraceFile", "trace.csv"),
            codec="vtm",
        )
    )

    cmd, log_file = runner.calls[0]
    assert cmd[:2] == [str(encoder), "-c"]
    assert "-wdt" in cmd and "512" in cmd
    assert "-q" in cmd and "32" in cmd
    assert "--InputChromaFormat=444" in cmd
    assert "--CSFScalingList=1" in cmd
    assert "--TraceFile" in cmd and "trace.csv" in cmd
    assert log_file == tmp_path / "encode.log"


def test_encoder_runner_builds_vtm_validation_command(tmp_path: Path) -> None:
    runner = RecordingRunner()
    encoder = Path("EncoderApp.exe")
    EncoderRunner(runner).encode(
        EncodeJob(
            encoder=encoder,
            yuv=Path("input.yuv"),
            width=512,
            height=512,
            qp=32,
            preset="medium",
            bitstream=tmp_path / "out.vvc",
            recon=tmp_path / "out.yuv",
            log=tmp_path / "encode.log",
            codec="vtm_validation",
        )
    )

    cmd, _log_file = runner.calls[0]
    assert cmd[:2] == [str(encoder), "-c"]
    assert "--InputChromaFormat=444" in cmd
    assert not any(arg.startswith("--CSFScalingList") for arg in cmd)


def test_encoder_runner_accepts_explicit_vtm_config(tmp_path: Path) -> None:
    runner = RecordingRunner()
    config = tmp_path / "study.cfg"
    EncoderRunner(runner).encode(
        EncodeJob(
            encoder=Path("EncoderApp.exe"),
            yuv=Path("input.yuv"),
            width=64,
            height=64,
            qp=32,
            preset="medium",
            bitstream=tmp_path / "out.vvc",
            recon=tmp_path / "out.yuv",
            log=tmp_path / "encode.log",
            codec="vtm",
            encoder_config=config,
        )
    )

    cmd, _log_file = runner.calls[0]
    assert cmd[:3] == ["EncoderApp.exe", "-c", str(config)]


def test_decoder_runner_builds_vvdec_command(tmp_path: Path) -> None:
    runner = RecordingRunner()
    decoder = platform_executable(Path("vvdecapp"))
    DecoderRunner(decoder, runner).decode(Path("in.vvc"), tmp_path / "decoded.yuv", tmp_path / "decode.log")

    cmd, log_file = runner.calls[0]
    assert cmd == [str(decoder), "-b", "in.vvc", "-o", str(tmp_path / "decoded.yuv")]
    assert log_file == tmp_path / "decode.log"
