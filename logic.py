"""Bounded local-file transcription; model inference never receives a source URL."""

from collections.abc import Mapping
import hashlib
import json
import math
import os
from contextlib import suppress
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import time

DEFAULTS = {"allowed_root": "", "language": "auto", "keywords": [], "word_timestamps": True,
            "download_missing": True, "max_duration_sec": 30, "max_file_bytes": 33554432,
            "timeout_sec": 120, "download_timeout_sec": 120}
LANGUAGES = ("auto", "en", "de", "fr", "es", "it", "nl", "pl")
FORMATS = {".wav", ".ogg", ".opus", ".webm", ".mp3", ".m4a", ".flac", ".mp4"}


class WhistleError(ValueError):
    """Safe diagnostic independent of document contents and native error strings."""


class Cancelled(WhistleError):
    pass


def configuration(raw):
    if not isinstance(raw, Mapping) or set(raw) - set(DEFAULTS) - {"execution", "position", "runtime_path", "runtime_path_label"}:
        raise WhistleError("Unsupported Whistle setting.")
    result = {**DEFAULTS, **{key: value for key, value in raw.items() if key in DEFAULTS}}
    if not isinstance(result["allowed_root"], str) or len(result["allowed_root"]) > 4096 or any(ord(char) < 32 for char in result["allowed_root"]):
        raise WhistleError("Choose a valid permitted audio directory.")
    if result["language"] not in LANGUAGES:
        raise WhistleError("Unsupported Whistle language.")
    for key in ("word_timestamps", "download_missing"):
        if type(result[key]) is not bool:
            raise WhistleError(f"{key} requires a boolean.")
    for key, minimum, maximum in (("max_duration_sec", 1, 30), ("max_file_bytes", 1024, 134217728),
                                  ("timeout_sec", 1, 300), ("download_timeout_sec", 10, 600)):
        if type(result[key]) is not int or not minimum <= result[key] <= maximum:
            raise WhistleError(f"{key} must be an integer between {minimum} and {maximum}.")
    values = result["keywords"]
    if not isinstance(values, (list, tuple)) or len(values) > 32 or any(not isinstance(item, str) or not item.strip()
        or len(item) > 100 or any(ord(char) < 32 for char in item) for item in values):
        raise WhistleError("Keywords require an array of up to 32 short words or phrases.")
    result["keywords"] = [value.strip() for value in values]
    try:
        if sum(len(value.encode("utf-8")) for value in result["keywords"]) > 4096:
            raise WhistleError("Vocabulary hints exceed 4 KiB.")
    except UnicodeError:
        raise WhistleError("Vocabulary hints must be valid UTF-8.") from None
    return result


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise WhistleError("Duplicate JSON property.")
            result[key] = value
        return result
    try:
        if len(raw.encode("utf-8")) > 16384:
            raise WhistleError("Input exceeds 16 KiB.")
        return json.loads(raw, object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(WhistleError("Finite JSON required.")))
    except (ValueError, UnicodeError, RecursionError):
        raise WhistleError("Expected a file path or a valid recording_ready event.") from None


def request(raw):
    """A finished-file event is distinct from a microphone start/stop command."""
    if isinstance(raw, str) and raw.lstrip().startswith("{"):
        raw = strict_json(raw)
    metadata = {}
    if isinstance(raw, dict):
        if raw.get("event") != "recording_ready":
            raise WhistleError("Send recording_ready after file finalization, not a stream start/stop command.")
        for key in ("recording_id", "stream_id", "call_id"):
            if key == "call_id" and key not in raw:
                continue
            value = raw.get(key)
            if not isinstance(value, str) or not 1 <= len(value) <= 128 or any(ord(char) < 32 for char in value):
                raise WhistleError("Invalid recording correlation.")
            metadata[key] = value
        if type(raw.get("bytes")) is not int or raw["bytes"] < 1:
            raise WhistleError("recording_ready requires the finalized file size.")
        metadata["bytes"] = raw["bytes"]
        raw = raw.get("path")
        if not isinstance(raw, str) or not Path(raw).is_absolute():
            raise WhistleError("recording_ready requires an absolute file path.")
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 4096 or any(ord(char) < 32 for char in raw):
        raise WhistleError("A nonempty local audio file path is required.")
    return raw.strip(), metadata


def check(deadline, cancel):
    if cancel():
        raise Cancelled("Transcription cancelled; no partial text was published.")
    if time.monotonic() >= deadline:
        raise WhistleError("Operation deadline exceeded; no partial text was published.")


def run_process(args, *, directory, deadline, cancel, limit, stdin=None):
    """Bound both output pipes in memory and terminate the entire owned process group."""
    environment = {key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "LC_ALL"}}
    environment.update(NEEDLE_TELEMETRY="0", DO_NOT_TRACK="1", HF_HUB_OFFLINE="1", PYTHONDONTWRITEBYTECODE="1")
    process = subprocess.Popen(args, cwd=directory, env=environment, start_new_session=True,
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True)
    output, errors = bytearray(), bytearray()
    selector = selectors.DefaultSelector()
    try:
        if stdin is not None:
            # Only a tiny trusted settings envelope, never audio, crosses stdin here.
            if len(stdin) > 8192:
                raise WhistleError("Internal settings envelope is too large.")
            process.stdin.write(stdin); process.stdin.close()
        for stream, name in ((process.stdout, "out"), (process.stderr, "err")):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        while selector.get_map():
            check(deadline, cancel)
            for key, _ in selector.select(timeout=0.03):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                target, maximum = (output, limit) if key.data == "out" else (errors, 16384)
                if len(target) + len(chunk) > maximum:
                    raise WhistleError("Audio tool or model exceeded its bounded output size.")
                target.extend(chunk)
        while process.poll() is None:
            check(deadline, cancel)
            time.sleep(0.02)
        check(deadline, cancel)
        if process.returncode:
            # Native errors can echo paths or arbitrary input: expose only owned safe codes.
            try:
                detail = json.loads(output).get("error_code")
            except (ValueError, AttributeError):
                detail = None
            messages = {"assets_missing": "Whistle weights/engine are missing; enable download or populate the block cache.",
                "assets_corrupt": "A cached Whistle asset failed its checksum; it was not loaded or overwritten.",
                "platform_unsupported": "This package supports Linux glibc on x86-64 or ARM64.",
                "download_failed": "The pinned Whistle model could not be downloaded and verified.",
                "model_failed": "Whistle could not load or transcribe the clip.",
                "cache_busy": "Another operation is preparing this block's model cache."}
            raise WhistleError(messages.get(detail, "Audio conversion or local model execution failed."))
        return bytes(output)
    finally:
        selector.close()
        if process.poll() is None:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=0.3)
        process.stdout.close(); process.stderr.close()
        if process.stdin is not None and not process.stdin.closed:
            process.stdin.close()


def snapshot(path, allowed, target, maximum, expected, deadline, cancel):
    """Keep the source unchanged and decode an immutable, private local copy."""
    source = (allowed / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
    if not source.is_relative_to(allowed) or source.suffix.lower() not in FORMATS:
        raise WhistleError("Audio must be a supported local file inside the permitted directory.")
    descriptor = os.open(source, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= maximum:
            raise WhistleError("Audio file is empty, oversized or not a regular file.")
        if expected is not None and before.st_size != expected:
            raise WhistleError("The completed recording size differs from its event.")
        digest, size = hashlib.sha256(), 0
        with open(target, "xb") as output:
            os.chmod(target, 0o600)
            while True:
                check(deadline, cancel)
                chunk = stream.read(min(65536, maximum + 1 - size))
                if not chunk:
                    break
                size += len(chunk)
                if size > maximum:
                    raise WhistleError("Audio file grew beyond its size limit.")
                digest.update(chunk); output.write(chunk)
        after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns) or size != before.st_size:
            raise WhistleError("The source changed while it was read; wait for file finalization.")
    return digest.hexdigest(), size


def validate_result(raw, duration, timestamps):
    """Validate only the native transcription contract, never execute model outputs."""
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get("text"), str) or len(data["text"]) > 32768:
            raise ValueError()
        if data.get("language") not in ("", *LANGUAGES[1:]):
            raise ValueError()
        result = {"text": data["text"], "language": data["language"]}
        for name in ("ttft_ms", "decode_tps"):
            value = data.get(name)
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError()
            result[name] = value
        if timestamps:
            words = data.get("words", [])
            if not isinstance(words, list) or len(words) > 1000:
                raise ValueError()
            previous = 0
            for word in words:
                if not isinstance(word, dict) or not isinstance(word.get("word"), str) or len(word["word"]) > 512:
                    raise ValueError()
                start, end, probability = (word.get(key) for key in ("start", "end", "probability"))
                if any(type(number) not in (int, float) or not math.isfinite(number) for number in (start, end, probability)):
                    raise ValueError()
                if not previous <= start <= end <= duration + 0.15 or not 0 <= probability <= 1:
                    raise ValueError()
                previous = start
            result["words"] = words
        return result
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise WhistleError("Whistle returned an invalid result; no partial text was published.") from None


def transcribe(raw, config, root, storage, cancel=lambda: False):
    config = configuration(config)
    path, correlation = request(raw)
    root, storage = Path(root), Path(storage)
    if not storage.is_absolute() or storage.is_symlink() or not storage.is_dir():
        raise WhistleError("A valid block-owned storage directory is required.")
    allowed = Path(config["allowed_root"]).expanduser() if config["allowed_root"] else root
    allowed = (root / allowed).resolve() if not allowed.is_absolute() else allowed.resolve()
    if not allowed.is_dir():
        raise WhistleError("The permitted audio directory does not exist.")
    started = time.monotonic()
    deadline = started + config["timeout_sec"]
    with tempfile.TemporaryDirectory(prefix="whistle-job-", dir=storage) as job:
        directory = Path(job)
        source, pcm = directory / "input.audio", directory / "input.f32"
        digest, size = snapshot(path, allowed, source, config["max_file_bytes"], correlation.get("bytes"), deadline, cancel)
        # Resample BEFORE counting the extra sample. Opus normally decodes at
        # 48 kHz; counting at that rate would silently shorten long recordings.
        maximum_samples = config["max_duration_sec"] * 16000
        audio = run_process(["ffmpeg", "-v", "error", "-nostdin", "-threads", "1", "-max_alloc", "67108864",
            "-format_whitelist", "wav,ogg,matroska,webm,mp3,mov,flac", "-protocol_whitelist", "file,pipe", "-i", str(source),
            "-map", "0:a:0", "-vn", "-sn", "-dn", "-ac", "1", "-ar", "16000", "-af", f"aresample=16000,atrim=end_sample={maximum_samples + 1}",
            "-f", "f32le", "pipe:1"], directory=directory, deadline=deadline, cancel=cancel, limit=(maximum_samples + 1) * 4)
        if not audio or len(audio) % 4:
            raise WhistleError("The file contains no decodable audio.")
        if len(audio) > maximum_samples * 4:
            raise WhistleError("Audio exceeds the configured limit (at most 30 seconds); split or shorten it first.")
        with open(pcm, "xb") as output:
            os.chmod(pcm, 0o600); output.write(audio)
        duration = len(audio) / 64000
        worker_config = {key: config[key] for key in ("language", "keywords", "word_timestamps", "download_missing", "timeout_sec", "download_timeout_sec")}
        job_config = {"storage": str(storage), "job": str(directory), "config": worker_config}
        # Download has its own explicit allowance; conversion/inference each stay time-bounded.
        raw_result = run_process([sys.executable, "-B", str(Path(__file__).with_name("worker.py"))],
            directory=directory, deadline=time.monotonic() + config["timeout_sec"] + config["download_timeout_sec"],
            cancel=cancel, limit=262144, stdin=json.dumps(job_config, ensure_ascii=False).encode())
        result = validate_result(raw_result, duration, config["word_timestamps"])
        result.update(duration_sec=duration, elapsed_sec=round(time.monotonic() - started, 4),
                      source={"sha256": digest, "bytes": size, **correlation}, model="Cactus-Compute/whistle",
                      model_version="2.0.0", engine_version="3.1.0", final=True)
        return result
