import os
import pathlib
import subprocess
import tempfile
import uuid
from typing import Any, Dict, Optional, cast
from urllib.parse import urlparse

import requests

try:
    import runpod  # type: ignore[import]
except ModuleNotFoundError:  # pragma: no cover - runpod only exists in the worker image
    runpod = cast(Any, None)

try:
    import boto3  # type: ignore[import]
    from botocore.config import Config as BotoConfig  # type: ignore[import]
except ModuleNotFoundError:  # pragma: no cover - boto3 only exists in the worker image
    boto3 = cast(Any, None)
    BotoConfig = cast(Any, None)

INPUT_KEY_FILE = "audio_file.mp3"
DEFAULT_MODEL = "htdemucs_ft"
DEFAULT_SHIFTS = 4
DEFAULT_OVERLAP = 0.25
DEFAULT_OUTPUT_FORMAT = "mp3"
DEFAULT_MP3_BITRATE = 320
# Stem formats Demucs can write, mapped to the Content-Type used for the R2 upload.
OUTPUT_CONTENT_TYPES = {"mp3": "audio/mpeg", "flac": "audio/flac", "wav": "audio/wav"}
NOTE_EVENTS_FILENAME = "note_events.csv"
DEFAULT_R2_PREFIX = "stems"
DEFAULT_URL_EXPIRY = 7 * 24 * 60 * 60  # SigV4 presigned URLs max out at 7 days
# Guards against oversized downloads; ~20 min of 320 kbps MP3. Override with MAX_INPUT_BYTES.
DEFAULT_MAX_INPUT_BYTES = 50 * 1024 * 1024


def _max_input_bytes() -> int:
    return int(os.environ.get("MAX_INPUT_BYTES", DEFAULT_MAX_INPUT_BYTES))


def _is_mp3(header: bytes) -> bool:
    # ID3v2 tag, or a bare MPEG audio frame sync (11 set bits).
    return header[:3] == b"ID3" or (len(header) >= 2 and header[0] == 0xFF and (header[1] & 0xE0) == 0xE0)


def _validate_mp3(path: pathlib.Path) -> None:
    with path.open("rb") as file_handle:
        if not _is_mp3(file_handle.read(3)):
            raise ValueError("Input must be an MP3 file")


def _download_audio(audio_url: str, destination: pathlib.Path) -> None:
    limit = _max_input_bytes()
    response = requests.get(audio_url, stream=True, timeout=120)
    response.raise_for_status()
    declared = int(response.headers.get("Content-Length") or 0)
    if declared > limit:
        raise ValueError(f"Input is {declared} bytes; the limit is {limit} bytes")
    written = 0
    with destination.open("wb") as file_handle:
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if chunk:
                written += len(chunk)
                if written > limit:
                    raise ValueError(f"Input exceeds the {limit} byte limit")
                file_handle.write(chunk)


def _is_public_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _find_stems_dir(out_root: pathlib.Path, model_name: str) -> pathlib.Path:
    model_root = out_root / model_name
    if not model_root.exists():
        raise FileNotFoundError(f"Demucs output missing for model '{model_name}'")
    candidates = [child for child in model_root.iterdir() if child.is_dir()]
    if not candidates:
        raise FileNotFoundError("No separated stems were produced")
    return candidates[0]


def _folder_name(value: Any) -> Optional[str]:
    # A single key segment; reusing the same name overwrites that folder's objects in R2.
    if value is None:
        return None
    name = str(value).strip()
    if not name:
        return None
    if "/" in name or "\\" in name or name in (".", ".."):
        raise ValueError("'name' must be a single folder name without '/' or '\\'")
    return name


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable {name}")
    return value


class R2Storage:
    """Thin wrapper around the S3-compatible Cloudflare R2 API."""

    def __init__(self) -> None:
        if boto3 is None:
            raise RuntimeError("boto3 is not installed; cannot upload stems to R2")
        account_id = _require_env("R2_ACCOUNT_ID")
        self.bucket = _require_env("R2_BUCKET")
        self.prefix = os.environ.get("R2_PREFIX", DEFAULT_R2_PREFIX).strip("/")
        self.public_base_url = os.environ.get("R2_PUBLIC_BASE_URL", "").rstrip("/") or None
        self.url_expiry = int(os.environ.get("R2_URL_EXPIRY", DEFAULT_URL_EXPIRY))
        self.client = boto3.client(
            "s3",
            endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=_require_env("R2_ACCESS_KEY_ID"),
            aws_secret_access_key=_require_env("R2_SECRET_ACCESS_KEY"),
            region_name="auto",
            config=BotoConfig(signature_version="s3v4"),
        )

    def key_for(self, folder: str, filename: str) -> str:
        return "/".join(part for part in (self.prefix, folder, filename) if part)

    def upload(self, path: pathlib.Path, key: str, content_type: str) -> None:
        self.client.upload_file(str(path), self.bucket, key, ExtraArgs={"ContentType": content_type})

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
        except Exception:  # pylint: disable=broad-except
            return False
        return True

    def url_for(self, key: str) -> str:
        if self.public_base_url:
            return f"{self.public_base_url}/{key}"
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=self.url_expiry,
        )


def _progress(event: Dict[str, Any], payload: Dict[str, Any]) -> None:
    # Surfaces in GET /status/{id} as `output` while the job is IN_PROGRESS.
    if runpod:
        runpod.serverless.progress_update(event, payload)


def _format_args(output_format: str, mp3_bitrate: int) -> list[str]:
    if output_format == "mp3":
        return ["--mp3", "--mp3-bitrate", str(mp3_bitrate)]
    if output_format == "flac":
        return ["--flac"]
    return []


def _upload_file(
    storage: R2Storage, path: pathlib.Path, folder: str, filename: str, content_type: str
) -> Dict[str, Optional[str]]:
    key = storage.key_for(folder, filename)
    storage.upload(path, key, content_type)
    # Only hand out a URL once the object is confirmed to be in R2.
    url = storage.url_for(key) if storage.exists(key) else None
    return {"filename": filename, "key": key, "url": url}


def _upload_stems(
    storage: R2Storage, stems_dir: pathlib.Path, folder: str, output_format: str
) -> Dict[str, Dict[str, Optional[str]]]:
    content_type = OUTPUT_CONTENT_TYPES[output_format]
    return {
        stem_file.stem: _upload_file(storage, stem_file, folder, stem_file.name, content_type)
        for stem_file in sorted(stems_dir.glob(f"*.{output_format}"))
    }


def _transcribe_notes(audio_path: pathlib.Path, out_dir: pathlib.Path, env: Dict[str, str]) -> pathlib.Path:
    # Spotify Basic Pitch writes <input stem>_basic_pitch.csv (plus a MIDI file we don't upload).
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = ["basic-pitch", str(out_dir), str(audio_path), "--save-note-events"]
    subprocess.run(cmd, check=True, env=env, cwd=out_dir)
    csv_files = sorted(out_dir.glob("*.csv"))
    if not csv_files:
        raise FileNotFoundError("Basic Pitch did not produce a note events CSV")
    return csv_files[0]


def handler(event: Dict[str, Any]) -> Dict[str, Any]:
    inputs = event.get("input") or {}
    job_id = str(event.get("id") or uuid.uuid4())

    audio_url_raw = inputs.get("audio_url")
    audio_url = audio_url_raw.strip() if isinstance(audio_url_raw, str) else ""
    model_name = inputs.get("model_name", DEFAULT_MODEL)
    shifts = int(inputs.get("shifts", DEFAULT_SHIFTS))
    overlap = float(inputs.get("overlap", DEFAULT_OVERLAP))
    output_format = str(inputs.get("output_format", DEFAULT_OUTPUT_FORMAT)).strip().lower()
    mp3_bitrate = int(inputs.get("mp3_bitrate", DEFAULT_MP3_BITRATE))

    if not _is_public_url(audio_url):
        # Returning an "error" key makes RunPod mark the job FAILED.
        return {"error": "Provide 'audio_url' as a publicly reachable http(s) URL to an MP3"}
    if output_format not in OUTPUT_CONTENT_TYPES:
        return {"error": f"'output_format' must be one of: {', '.join(OUTPUT_CONTENT_TYPES)}"}
    try:
        name = _folder_name(inputs.get("name"))
    except ValueError as exc:
        return {"error": str(exc)}
    folder = name or job_id

    base_payload: Dict[str, Any] = {
        "job_id": job_id,
        "name": name,
        "model": model_name,
        "shifts": shifts,
        "overlap": overlap,
        "output_format": output_format,
    }
    if output_format == "mp3":
        base_payload["mp3_bitrate"] = mp3_bitrate

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = pathlib.Path(tmp_dir)
        audio_path = tmp_path / INPUT_KEY_FILE
        output_root = tmp_path / "separations"
        notes_root = tmp_path / "notes"

        try:
            storage = R2Storage()

            _progress(event, {**base_payload, "stage": "downloading", "stems": None})
            _download_audio(audio_url, audio_path)
            _validate_mp3(audio_path)

            env = os.environ.copy()
            env.setdefault("TORCHAUDIO_USE_SOUND_FILE", "1")

            cmd = [
                "demucs",
                "--name",
                model_name,
                "--shifts",
                str(shifts),
                "--overlap",
                str(overlap),
                *_format_args(output_format, mp3_bitrate),
                "--out",
                str(output_root),
                str(audio_path),
            ]

            _progress(event, {**base_payload, "stage": "separating", "stems": None})
            subprocess.run(cmd, check=True, env=env, cwd=tmp_dir)

            stems_dir = _find_stems_dir(output_root, model_name)

            _progress(event, {**base_payload, "stage": "transcribing", "stems": None})
            note_events_path = _transcribe_notes(audio_path, notes_root, env)

            _progress(event, {**base_payload, "stage": "uploading", "stems": None})
            stems_payload = _upload_stems(storage, stems_dir, folder, output_format)
            if not stems_payload:
                return {"error": "No stems were produced"}
            missing = [stem_name for stem_name, stem in stems_payload.items() if not stem["url"]]
            if missing:
                return {"error": f"Stems not found in R2 after upload: {', '.join(missing)}"}
            note_events = _upload_file(storage, note_events_path, folder, NOTE_EVENTS_FILENAME, "text/csv")
            if not note_events["url"]:
                return {"error": "Note events CSV not found in R2 after upload"}

            return {
                **base_payload,
                "status": "success",
                "stage": "done",
                "stem_count": len(stems_payload),
                "stems": stems_payload,
                "note_events": note_events,
            }
        except Exception as exc:  # pylint: disable=broad-except
            return {"error": str(exc)}


if runpod:
    runpod.serverless.start({"handler": handler})
