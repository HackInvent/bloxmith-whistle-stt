"""Portable, offline-only real-model fixture; no production audio or caches are read."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import wave


def install_cache(storage):
    location = os.environ.get("WHISTLE_TEST_CACHE", "")
    assert location, "Prepare the pinned fixture with setup_model.py and set WHISTLE_TEST_CACHE to its whistle-cache directory"
    source = Path(location)
    assert source.is_dir() and not source.is_symlink()
    from blocs.whistle_stt.worker import verified
    manifest = json.loads(Path(__file__).parents[1].joinpath("model_artifacts.json").read_text(encoding="utf-8"))
    target = storage / "whistle-cache"
    target.mkdir(parents=True)
    for spec in [manifest["weights"], *manifest["engines"].values()]:
        path = source / spec["filename"]
        if path.exists():
            assert verified(path, spec)
            shutil.copy2(path, target / spec["filename"])


def speech(directory, *, suffix=".wav"):
    path = directory / ("speech" + suffix)
    command = ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "flite=text=The weather is nice today.:voice=slt",
               "-ac", "1", "-ar", "16000"]
    if suffix in (".ogg", ".webm"):
        command.extend(["-c:a", "libopus"])
    subprocess.run([*command, str(path)], check=True, timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return path


def silence(path, seconds):
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1); output.setsampwidth(2); output.setframerate(16000)
        output.writeframes(b"\0" * (int(seconds * 16000) * 2))
