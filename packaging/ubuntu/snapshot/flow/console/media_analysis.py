"""Prepare bounded local image/video samples; never contacts a provider."""
import base64
import io
import json
import math
import re
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path

from PIL import Image, ImageOps


def ffmpeg_executable():
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        raise ValueError("视频抽帧组件未安装，未调用分析模型。") from None


def audio_duration(path, mime):
    """Read local audio metadata in seconds without decoding the full file."""
    if mime == "audio/wav":
        try:
            with wave.open(str(path), "rb") as audio:
                frames = audio.getnframes()
                if frames and frames * audio.getsampwidth() * audio.getnchannels() <= Path(path).stat().st_size:
                    return frames / audio.getframerate()
        except (OSError, EOFError, wave.Error):
            pass
    try:
        result = subprocess.run(
            [ffmpeg_executable(), "-nostdin", "-hide_banner", "-protocol_whitelist", "file",
             "-f", "wav" if mime == "audio/wav" else "mov", "-i", str(path)],
            capture_output=True, timeout=8,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    match = re.search(rb"Duration: (\d+):(\d{2}):(\d{2}(?:\.\d+)?)", result.stderr)
    if match and b"Audio:" in result.stderr:
        hours, minutes, seconds = (float(value) for value in match.groups())
        return hours * 3600 + minutes * 60 + seconds or None
    return None


def image_url(path, max_side):
    with Image.open(path) as original:
        image = ImageOps.exif_transpose(original)
        image.thumbnail((max_side, max_side))
        image = image.convert("RGB")
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=82)
    return "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode("ascii")


def prepare_media(path, media_type, frame_count=4, max_side=1024):
    if not path or not Path(path).is_file():
        raise ValueError("素材原文件不存在，未调用分析模型。")
    if media_type == "image":
        return [image_url(path, max_side)], {"media_type": "image", "sample_count": 1}
    if media_type != "video":
        raise ValueError("暂只支持图片和视频。")
    executable = ffmpeg_executable()
    probe = subprocess.run([executable, "-nostdin", "-hide_banner", "-i", str(path)],
                           capture_output=True, timeout=20)
    match = re.search(rb"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", probe.stderr)
    if not match:
        raise ValueError("视频时长无法确认，未调用分析模型。")
    duration = float(match[1]) * 3600 + float(match[2]) * 60 + float(match[3])
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("视频时长无法确认，未调用分析模型。")
    # Midpoints of equal intervals avoid an empty first/last frame.
    timestamps = [round(duration * (i + .5) / frame_count, 3) for i in range(frame_count)]
    urls = []
    with tempfile.TemporaryDirectory(prefix="material-frames-") as folder:
        for i, timestamp in enumerate(timestamps):
            target = Path(folder) / f"{i}.jpg"
            subprocess.run([
                executable, "-nostdin", "-v", "error", "-ss", str(timestamp), "-i", str(path),
                "-frames:v", "1", "-vf", f"scale={max_side}:{max_side}:force_original_aspect_ratio=decrease",
                "-threads", "1", "-y", str(target),
            ], capture_output=True, check=True, timeout=30)
            urls.append(image_url(target, max_side))
    return urls, {"media_type": "video", "sample_count": len(urls), "frame_seconds": timestamps,
                  "duration_seconds": duration, "scope": "sampled_frames_only_no_audio"}
