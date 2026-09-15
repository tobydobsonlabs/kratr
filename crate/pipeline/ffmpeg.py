"""Thin subprocess wrappers around ffmpeg/ffprobe.

Both binaries are located via PATH rather than hardcoded — on this machine they sit
under a WinGet package directory whose name changes with every ffmpeg update.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any

from .. import config

logger = logging.getLogger(__name__)

#: Stops a console window flashing up on Windows for every subprocess call.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class FFmpegError(RuntimeError):
    """ffmpeg or ffprobe exited non-zero."""

    def __init__(self, cmd: list[str], returncode: int, stderr: str) -> None:
        self.cmd = cmd
        self.returncode = returncode
        self.stderr = stderr
        tail = "\n".join(stderr.strip().splitlines()[-6:])
        super().__init__(f"{Path(cmd[0]).name} failed ({returncode}):\n{tail}")


class FFmpegMissingError(RuntimeError):
    pass


def _binary(name: str, configured: str | None) -> str:
    path = config.find_executable(name, configured)
    if not path:
        raise FFmpegMissingError(
            f"{name} not found. Install it and make sure it is on PATH, "
            f"or set its location in settings."
        )
    return path


def ffmpeg_path(settings: config.Settings | None = None) -> str:
    return _binary("ffmpeg", settings.ffmpeg_path if settings else None)


def ffprobe_path(settings: config.Settings | None = None) -> str:
    return _binary("ffprobe", settings.ffprobe_path if settings else None)


def run(cmd: list[str], *, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    """Run a command, raising :class:`FFmpegError` on failure."""
    logger.debug("run: %s", " ".join(cmd))
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=_NO_WINDOW,
        check=False,
    )
    if proc.returncode != 0:
        raise FFmpegError(cmd, proc.returncode, proc.stderr or "")
    return proc


def run_binary(cmd: list[str], *, timeout: float | None = None) -> bytes:
    """Run a command and return raw stdout bytes (for PCM piping)."""
    logger.debug("run(binary): %s", " ".join(cmd))
    proc = subprocess.run(
        cmd,
        capture_output=True,
        timeout=timeout,
        creationflags=_NO_WINDOW,
        check=False,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "replace")
        raise FFmpegError(cmd, proc.returncode, stderr)
    return proc.stdout


def decode_pcm(
    path: Path,
    *,
    sample_rate: int,
    channels: int = 1,
    max_seconds: float | None = None,
    settings: config.Settings | None = None,
) -> bytes:
    """Decode to raw float32 PCM.

    Used both for spectral analysis and for null-testing a conversion against its
    source — the check that proves a "lossless" conversion really was.
    """
    cmd = [ffmpeg_path(settings), "-v", "error"]
    if max_seconds is not None:
        cmd += ["-t", str(max_seconds)]
    cmd += [
        "-i", str(path),
        "-ac", str(channels),
        "-ar", str(sample_rate),
        "-f", "f32le",
        "-",
    ]
    return run_binary(cmd, timeout=900)


def probe_json(path: Path, settings: config.Settings | None = None) -> dict[str, Any]:
    """Raw ffprobe output for a media file."""
    cmd = [
        ffprobe_path(settings),
        "-v", "error",
        "-show_streams",
        "-show_format",
        "-print_format", "json",
        str(path),
    ]
    proc = run(cmd, timeout=60)
    return json.loads(proc.stdout)
