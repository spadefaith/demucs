import pathlib
import time
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import requests
import typer
from dynaconf import Dynaconf

DEFAULT_MODEL = "htdemucs_ft"
DEFAULT_SHIFTS = 4
DEFAULT_OVERLAP = 0.25
DEFAULT_OUTPUT_FORMAT = "mp3"
DEFAULT_MP3_BITRATE = 320
OUTPUT_FORMATS = ("mp3", "flac", "wav")
DEFAULT_API_BASE = "https://api.runpod.ai/v2"
TERMINAL_FAILURES = ("FAILED", "CANCELLED", "TIMED_OUT")

settings = Dynaconf(envvar_prefix="RUNPOD", environments=True, load_dotenv=True)

app = typer.Typer(help="Submit async Demucs jobs to RunPod and fetch the stem URLs from R2.")


def _is_public_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _download_files(files: Dict[str, Any], destination: pathlib.Path, timeout: int) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for key, value in files.items():
        url = value.get("url") if isinstance(value, dict) else None
        if not url:
            continue
        filename = value.get("filename") or pathlib.PurePosixPath(urlparse(url).path).name or key
        output_path = destination / filename
        with requests.get(url, stream=True, timeout=timeout) as response:
            response.raise_for_status()
            with output_path.open("wb") as file_handle:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        file_handle.write(chunk)
        typer.echo(f"wrote {output_path}")


def _resolve_option(value: Optional[str], setting_key: str) -> Optional[str]:
    if value:
        return value
    resolved = settings.get(setting_key)
    if isinstance(resolved, str) and resolved.strip():
        return resolved
    return None


def _fail(message: str) -> None:
    typer.secho(message, err=True, fg=typer.colors.RED)
    raise typer.Exit(code=1)


def _request(method: str, url: str, headers: Dict[str, str], timeout: int, **kwargs: Any) -> Dict[str, Any]:
    try:
        response = requests.request(method, url, headers=headers, timeout=timeout, **kwargs)
        response.raise_for_status()
    except requests.RequestException as exc:
        typer.secho(f"RunPod request failed: {exc}", err=True, fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    return response.json()


def _stems_ready(output: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(output, dict):
        return None
    stems = output.get("stems")
    if not isinstance(stems, dict) or not stems:
        return None
    if all(isinstance(stem, dict) and stem.get("url") for stem in stems.values()):
        return stems
    return None


@app.command()
def main(
    api_key: Optional[str] = typer.Option(None, help="RunPod API token"),
    endpoint_id: Optional[str] = typer.Option(None, help="RunPod endpoint ID"),
    endpoint_url: Optional[str] = typer.Option(
        None, help="Override the endpoint base URL (e.g. https://api.runpod.ai/v2/<id>)"
    ),
    api_base: str = typer.Option(DEFAULT_API_BASE, help="Base URL for RunPod API"),
    audio_url: Optional[str] = typer.Option(None, help="Publicly reachable http(s) URL of the MP3 to separate"),
    job_id: Optional[str] = typer.Option(None, help="Resume polling an already-submitted job"),
    model_name: str = typer.Option(DEFAULT_MODEL),
    shifts: int = typer.Option(DEFAULT_SHIFTS),
    overlap: float = typer.Option(DEFAULT_OVERLAP),
    output_format: str = typer.Option(DEFAULT_OUTPUT_FORMAT, help="Stem format: mp3, flac, or wav"),
    mp3_bitrate: int = typer.Option(DEFAULT_MP3_BITRATE, help="Bitrate (kbps) when --output-format is mp3"),
    wait: bool = typer.Option(True, help="Poll until the stems are in R2; --no-wait just prints the job ID"),
    poll_interval: float = typer.Option(5.0, help="Seconds between status checks"),
    max_wait: int = typer.Option(3600, help="Give up polling after this many seconds"),
    timeout: int = typer.Option(120, help="HTTP timeout in seconds per request"),
    download: bool = typer.Option(True, help="Download the stems from their R2 URLs once ready"),
    save_dir: pathlib.Path = typer.Option(pathlib.Path("runpod-stems"), help="Destination directory"),
) -> None:
    """Submit a job to the RunPod Demucs worker (async) and report the R2 stem URLs."""

    resolved_api_key = _resolve_option(api_key, "API_KEY")
    if not resolved_api_key:
        raise typer.BadParameter("Set --api-key or configure RUNPOD_API_KEY", param_hint="--api-key")

    resolved_endpoint_url = _resolve_option(endpoint_url, "ENDPOINT_URL")
    resolved_endpoint_id = _resolve_option(endpoint_id, "ENDPOINT_ID")
    if not (resolved_endpoint_url or resolved_endpoint_id):
        raise typer.BadParameter(
            "Provide --endpoint-url or configure --endpoint-id/RUNPOD_ENDPOINT_ID",
            param_hint="--endpoint-url / --endpoint-id",
        )

    base_url = (resolved_endpoint_url or f"{api_base.rstrip('/')}/{resolved_endpoint_id}").rstrip("/")
    headers = {"Authorization": f"Bearer {resolved_api_key}", "Content-Type": "application/json"}

    if not job_id:
        if not audio_url:
            raise typer.BadParameter("Provide --audio-url (or --job-id to resume)", param_hint="--audio-url")
        if not _is_public_url(audio_url):
            raise typer.BadParameter("Must be an http(s) URL", param_hint="--audio-url")
        if output_format not in OUTPUT_FORMATS:
            raise typer.BadParameter(f"Must be one of: {', '.join(OUTPUT_FORMATS)}", param_hint="--output-format")
        payload: Dict[str, Any] = {
            "audio_url": audio_url,
            "model_name": model_name,
            "shifts": shifts,
            "overlap": overlap,
            "output_format": output_format,
            "mp3_bitrate": mp3_bitrate,
        }

        body = _request("POST", f"{base_url}/run", headers, timeout, json={"input": payload})
        job_id = body.get("id")
        if not job_id:
            _fail(f"RunPod did not return a job ID: {body}")
        typer.secho(f"submitted job {job_id} ({body.get('status')})", fg=typer.colors.CYAN)

    if not wait:
        typer.echo(f"resume later with: runpod-demucs --job-id {job_id}")
        return

    deadline = time.monotonic() + max_wait
    last_stage: Optional[str] = None
    stems: Optional[Dict[str, Any]] = None
    note_events: Optional[Dict[str, Any]] = None
    while time.monotonic() < deadline:
        body = _request("GET", f"{base_url}/status/{job_id}", headers, timeout)
        status = body.get("status")
        output = body.get("output")

        if status in TERMINAL_FAILURES:
            error = body.get("error") or (output.get("error") if isinstance(output, dict) else output)
            _fail(f"Job {job_id} {status}: {error}")

        stage = output.get("stage") if isinstance(output, dict) else None
        if (stage or status) != last_stage:
            typer.echo(f"{status}: {stage or 'stems=null'}")
            last_stage = stage or status

        if status == "COMPLETED":
            stems = _stems_ready(output)
            if stems is None:
                _fail(f"Job {job_id} completed without stem URLs: {output}")
            raw_note_events = output.get("note_events") if isinstance(output, dict) else None
            if isinstance(raw_note_events, dict) and raw_note_events.get("url"):
                note_events = raw_note_events
            break

        time.sleep(poll_interval)

    if stems is None:
        _fail(f"Timed out waiting for job {job_id}; resume with --job-id {job_id}")
        return

    for name, stem in stems.items():
        typer.echo(f"{name}: {stem['url']}")
    if note_events:
        typer.echo(f"note_events: {note_events['url']}")

    if download:
        destination = save_dir.expanduser().resolve()
        _download_files(stems, destination, timeout)
        if note_events:
            _download_files({"note_events": note_events}, destination, timeout)
        typer.secho(f"Downloaded {len(stems)} stems to {destination}", fg=typer.colors.GREEN)


if __name__ == "__main__":
    app()
