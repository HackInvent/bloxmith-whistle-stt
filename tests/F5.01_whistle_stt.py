"""FB1/FB2/FB3/FB4: real CPU inference, strict local files, cache integrity and cancellation."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time
import wave
from types import MappingProxyType

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "tests"), str(Path(__file__).parent)]
from blocs.whistle_stt import logic
from blocs.whistle_stt.worker import AssetError, verified, PinnedRedirects
from whistle_fixture import install_cache, speech, silence


def refused(action):
    try:
        action()
    except (logic.WhistleError, AssetError):
        return
    raise AssertionError("Invalid input or asset was accepted")


def main():
    for value in ({"api_key": "no"}, {"language": "xx"}, {"language": {}}, {"max_duration_sec": 31},
        {"word_timestamps": "true"}, {"keywords": ["line\nbreak"]}, {"keywords": ["\ud800"]}, {"timeout_sec": True}):
        refused(lambda: logic.configuration(value))
    assert logic.configuration(MappingProxyType({"keywords": ("BloxSmith",)}))["keywords"] == ["BloxSmith"]
    for value in (None, "", {"action": "stop"}, '{"event":"x","event":"recording_ready"}', "https://invalid/audio.wav\n"):
        refused(lambda: logic.request(value))
    redirect = PinnedRedirects()
    from urllib.request import Request
    for address in ("http://huggingface.co/weights", "https://other.invalid/weights", "https://huggingface.co.evil.invalid/weights"):
        refused(lambda: redirect.redirect_request(Request("https://huggingface.co/model"), None, 302, "", {}, address))
    with TemporaryDirectory(prefix="whistle-real-") as temporary:
        root = Path(temporary)
        storage = root / "storage"; storage.mkdir()
        install_cache(storage)
        config = {"allowed_root": str(root), "language": "en", "download_missing": False}
        wav = speech(root)
        original = wav.read_bytes()
        result = logic.transcribe(str(wav), config, root, storage)
        assert "weather" in result["text"].lower() and "nice" in result["text"].lower(), result
        assert result["language"] == "en" and result["words"] and result["final"] is True
        assert result["source"]["sha256"] == hashlib.sha256(original).hexdigest()
        assert wav.read_bytes() == original and not list(storage.glob("whistle-job-*"))
        opus = speech(root, suffix=".ogg")
        event = {"event": "recording_ready", "recording_id": "recording-1", "stream_id": "stream-1", "call_id": "call-1",
                 "path": str(opus), "bytes": opus.stat().st_size}
        recorded = logic.transcribe(json.dumps(event), {**config, "word_timestamps": False}, root, storage)
        assert "weather" in recorded["text"].lower() and "words" not in recorded
        assert recorded["source"]["call_id"] == "call-1"
        refused(lambda: logic.transcribe(str(opus), {**config, "max_duration_sec": 1}, root, storage))
        # Enforce the duration at the model rate, not at the input container's
        # rate: both low-rate WAV and 48 kHz Opus previously escaped this gate.
        for rate in (8000, 16000, 44100, 48000):
            clip = root / f"rate-{rate}.wav"
            for seconds in (1, 2):
                with wave.open(str(clip), "wb") as output:
                    output.setnchannels(1); output.setsampwidth(2); output.setframerate(rate)
                    output.writeframes(b"\0\0" * (rate * seconds))
                bounded = lambda: logic.transcribe(str(clip), {**config, "max_duration_sec": 1}, root, storage)
                if seconds == 1:
                    assert bounded()["duration_sec"] == 1, rate
                else:
                    refused(bounded)
        extra = root / "one-extra-sample.wav"
        with wave.open(str(extra), "wb") as output:
            output.setnchannels(1); output.setsampwidth(2); output.setframerate(16000)
            output.writeframes(b"\0\0" * 16001)
        refused(lambda: logic.transcribe(str(extra), {**config, "max_duration_sec": 1}, root, storage))
        refused(lambda: logic.transcribe({**event, "bytes": 1}, config, root, storage))
        silent = root / "silence.wav"; silence(silent, 1)
        quiet = logic.transcribe(str(silent), config, root, storage)
        assert quiet["text"] == "" and quiet["language"] == "", quiet
        long = root / "long.wav"; silence(long, 30.1)
        refused(lambda: logic.transcribe(str(long), config, root, storage))
        refused(lambda: logic.transcribe(str(wav), {**config, "max_file_bytes": 1024}, root, storage))
        refused(lambda: logic.transcribe(str(wav), {**config, "allowed_root": str(storage)}, root, storage))
        playlist = root / "playlist.wav"
        playlist.write_text("ffconcat version 1.0\nfile '" + str(wav) + "'\n", encoding="utf-8")
        refused(lambda: logic.transcribe(str(playlist), config, root, storage))
        before = time.monotonic()
        refused(lambda: logic.run_process([sys.executable, "-c", "import time; time.sleep(20)"], directory=root,
            deadline=before + 10, cancel=lambda: time.monotonic() > before + 0.1, limit=1024))
        assert time.monotonic() - before < 1
        refused(lambda: logic.run_process([sys.executable, "-c", "print('x'*10000)"], directory=root,
            deadline=time.monotonic() + 5, cancel=lambda: False, limit=1024))
        empty = root / "empty-cache"; empty.mkdir()
        refused(lambda: logic.transcribe(str(wav), config, root, empty))
        asset = storage / "whistle-cache" / "whistle-2.0.0.cact"
        with asset.open("r+b") as stream:
            stream.seek(128); stream.write(b"INVALID")
        corrupt = asset.read_bytes()
        refused(lambda: logic.transcribe(str(wav), config, root, storage))
        assert asset.read_bytes() == corrupt, "A corrupt cache must never be silently overwritten"
        assert not list(storage.glob("whistle-job-*"))
    print("[ok] Real Whistle speech/silence/Ogg inference, word times, strict clip limit, offline cache, cancellation")


if __name__ == "__main__":
    main()
