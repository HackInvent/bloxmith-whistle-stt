"""Private, killable process using the official Needle speech C API.

The model is process-global and not thread safe: never load it in the framework process.
Only pinned public model assets may be downloaded, never user audio.
"""

import array
import ctypes
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import resource
import stat
import sys
import time
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import zipfile


class AssetError(ValueError):
    pass


class PinnedRedirects(HTTPRedirectHandler):
    """Hugging Face's asset CDN is allowed; downgrade and arbitrary redirects are refused."""
    max_redirections = 5
    max_repeats = 2

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlparse(newurl)
        host = parsed.hostname or ""
        if parsed.scheme != "https" or parsed.username or parsed.password or not (
            host == "huggingface.co" or host.endswith(".huggingface.co") or host.endswith(".hf.co")):
            raise AssetError("download_failed")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def verified(path, spec):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False
    except OSError:
        raise AssetError("assets_corrupt") from None
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size != spec["bytes"]:
            raise AssetError("assets_corrupt")
        digest = hashlib.sha256()
        while chunk := stream.read(65536):
            digest.update(chunk)
        if digest.hexdigest() != spec["sha256"]:
            raise AssetError("assets_corrupt")
    return True


def asset(spec, cache, job, allowed, deadline):
    target = cache / spec["filename"]
    if verified(target, spec):
        return target
    if not allowed:
        raise AssetError("assets_missing")
    staging = job / (spec["filename"] + ".part")
    try:
        opener = build_opener(ProxyHandler({}), PinnedRedirects())
        request = Request(spec["url"], headers={"User-Agent": "BloxSmith-Whistle-STT/0.1.0", "Accept-Encoding": "identity"})
        with opener.open(request, timeout=min(10, max(0.1, deadline - time.monotonic()))) as source, open(staging, "xb") as output:
            os.chmod(staging, 0o600)
            total = 0
            while True:
                if time.monotonic() >= deadline:
                    raise AssetError("download_failed")
                chunk = source.read(min(65536, spec["bytes"] + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > spec["bytes"]:
                    raise AssetError("download_failed")
                output.write(chunk)
            output.flush(); os.fsync(output.fileno())
        verified(staging, spec)
        # The owned cache lock serializes all downloads and never replaces an existing asset.
        os.link(staging, target, follow_symlinks=False)
        staging.unlink()
        return target
    except Exception:
        raise AssetError("download_failed") from None


def prepare(storage, job, config):
    manifest = json.loads(Path(__file__).with_name("model_artifacts.json").read_text(encoding="utf-8"))
    machine = platform.machine().lower()
    if machine == "arm64":
        machine = "aarch64"
    target = sys.platform + "-" + machine
    if sys.platform != "linux" or platform.libc_ver()[0] != "glibc" or target not in manifest["engines"]:
        raise AssetError("platform_unsupported")
    cache = storage / "whistle-cache"
    cache.mkdir(mode=0o700, exist_ok=True)
    if cache.is_symlink() or not cache.is_dir():
        raise AssetError("assets_corrupt")
    lock = os.open(cache / "assets.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(lock).st_mode):
            raise AssetError("assets_corrupt")
        deadline = time.monotonic() + config["download_timeout_sec"]
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AssetError("cache_busy") from None
        weights = asset(manifest["weights"], cache, job, config["download_missing"], deadline)
        wheel = asset(manifest["engines"][target], cache, job, config["download_missing"], deadline)
    finally:
        os.close(lock)
    with zipfile.ZipFile(wheel) as archive:
        matches = [info for info in archive.infolist() if info.filename == "needle/libneedle3.so"]
        if len(matches) != 1 or not 0 < matches[0].file_size <= 8388608:
            raise AssetError("assets_corrupt")
        library = job / "libneedle.so"
        with open(library, "xb") as output:
            os.chmod(library, 0o600)
            output.write(archive.read(matches[0]))
    return weights, library


def infer(weights, library, pcm, config):
    """Exactly the documented needle_load / needle_models / needle_transcribe ABI."""
    resource.setrlimit(resource.RLIMIT_AS, (1024 * 1024 * 1024, 1024 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CPU, (config["timeout_sec"], config["timeout_sec"] + 1))
    lib = ctypes.CDLL(str(library))
    lib.needle_load.argtypes = [ctypes.c_char_p, ctypes.c_uint64]
    lib.needle_load.restype = ctypes.c_int
    lib.needle_models.argtypes, lib.needle_models.restype = [], ctypes.c_int
    lib.needle_transcribe.argtypes = [ctypes.POINTER(ctypes.c_float), ctypes.c_int, ctypes.c_char_p,
        ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    lib.needle_transcribe.restype = ctypes.c_int
    raw = weights.read_bytes()
    if lib.needle_load(raw, len(raw)) < 0 or lib.needle_models() != 2:
        raise AssetError("model_failed")
    samples = array.array("f")
    samples.frombytes(pcm.read_bytes())
    if sys.byteorder != "little":
        samples.byteswap()
    if not 0 < len(samples) <= 480000 or any(not math.isfinite(value) or abs(value) > 1.001 for value in samples):
        raise AssetError("model_failed")
    data = (ctypes.c_float * len(samples)).from_buffer(samples)
    result = ctypes.create_string_buffer(262144)
    language = None if config["language"] == "auto" else config["language"].encode("utf-8")
    keywords = "\n".join(config["keywords"]).encode("utf-8") or None
    if lib.needle_transcribe(data, len(samples), language, keywords, int(config["word_timestamps"]), result, len(result)) < 0:
        raise AssetError("model_failed")
    return result.value


def main():
    job_data = json.loads(sys.stdin.buffer.read(8193))
    config = job_data["config"]
    storage, job = Path(job_data["storage"]), Path(job_data["job"])
    os.environ["NEEDLE_TELEMETRY"] = "0"
    os.environ["DO_NOT_TRACK"] = "1"
    weights, library = prepare(storage, job, config)
    sys.stdout.buffer.write(infer(weights, library, job / "input.f32", config))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        code = str(error) if isinstance(error, AssetError) else "model_failed"
        print(json.dumps({"error_code": code}))
        raise SystemExit(1) from None
