# Demucs serverless toolkit

This repository now acts as a monorepo for three complementary flows that all rely on the root-level `pyproject.toml`:

1. **`local-run/`** – documentation plus a scratch space for running the official Demucs CLI locally (outputs land here).
2. **`runpod-worker/`** – the GPU-ready serverless worker that RunPod clones, builds, and deploys.
3. **`client/`** – a thin command-line helper that submits async jobs and fetches the stems from R2.

## Repository layout

| Path | Purpose |
| --- | --- |
| `local-run/` | Docs + separation outputs for local experimentation; uses the root `pyproject.toml`. |
| `runpod-worker/` | Dockerfile, handler, and `runpod.yaml` describing the serverless worker. |
| `client/` | Python CLI (`runpod-demucs`) that calls the deployed endpoint and writes WAVs. |
| `AGENTS.md` | Contributor and agent workflow guide for this repo. |

UV is configured (via `uv.toml`) to keep its cache in `.uv/cache`, ensuring everything lives inside the repo; the `.uv/` directory is ignored by git.

## Pre-commit + linting

We gate merges with a `pre-commit` job that runs the standard formatting hooks plus `ruff` and `pyrefly` type checking. Run these locally before pushing:

```bash
uv run pre-commit install      # one-time git hook install
uv run pre-commit run --all-files
```

CI mirrors the same command in `.github/workflows/pre-commit.yml`. Mark the resulting **pre-commit** GitHub check as required in branch protections so pull requests must pass before merging into `master`/`main`.

## Local development (`local-run/`)

The existing workflow moved intact under `local-run/`, but everything is powered by the repo-root `pyproject.toml`. Quick start:

```bash
cd local-run
uv sync
TORCHAUDIO_USE_SOUND_FILE=1 uv run demucs --name htdemucs ~/Downloads/song.wav
```

Outputs land in `local-run/separations/<timestamp>/...`. The subdirectory README covers the details.

## RunPod serverless worker (`runpod-worker/`)

Key files:

- `Dockerfile` – installs CUDA-enabled PyTorch 2.4.1/torchaudio 2.4.1, Demucs, ffmpeg, and the RunPod SDK.
- `handler.py` – downloads the MP3 from a public `audio_url`, runs `demucs --name <model> --shifts --overlap`, uploads each stem to Cloudflare R2, and returns the stem URLs.
- `runpod.yaml` – instructs RunPod to launch `handler.py` and call its `handler` function.

Build/test locally:

```bash
cd runpod-worker
docker build -t demucs-worker .
docker run --rm demucs-worker  # RunPod will pass events, but this validates the image
```

Deploy checklist:

1. Push this repo to GitHub (public or private works).
2. In the RunPod dashboard choose **Deploy Serverless Endpoint → Connect GitHub** and select the repo.
3. Set the R2 environment variables below on the endpoint and deploy.
4. RunPod returns an endpoint ID such as `8cw1xzsn9rmbti`. Use it with `client/runpod_client.py` or plain curl.

| Variable | Required | Purpose |
| --- | --- | --- |
| `R2_ACCOUNT_ID` | yes | Cloudflare account ID (endpoint is `https://<id>.r2.cloudflarestorage.com`). |
| `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` | yes | R2 API token with write access to the bucket. |
| `R2_BUCKET` | yes | Bucket that receives the stems. |
| `R2_PREFIX` | no | Key prefix, default `stems` → `stems/<job_id>/vocals.wav`. |
| `R2_PUBLIC_BASE_URL` | no | Public bucket/custom domain; when set, URLs are `<base>/<key>` instead of presigned. |
| `R2_URL_EXPIRY` | no | Presigned URL lifetime in seconds (default and max: 604800 = 7 days). |

### Async flow

Submit with `/run`; it returns a job ID immediately. Poll `/status/<job_id>`: while the job runs,
`output` is either absent or a progress payload with `"stems": null` and a `stage`
(`downloading` → `separating` → `uploading`). Each stem is uploaded and confirmed in R2 (`HEAD`)
before its URL is handed out, so once the status is `COMPLETED` the output is:

```json
{
  "status": "success",
  "stage": "done",
  "job_id": "abc-123",
  "model": "htdemucs_ft",
  "stem_count": 4,
  "stems": {
    "vocals": {"filename": "vocals.wav", "key": "stems/abc-123/vocals.wav", "url": "https://..."},
    "drums": {"filename": "drums.wav", "key": "stems/abc-123/drums.wav", "url": "https://..."},
    "bass": {"filename": "bass.wav", "key": "stems/abc-123/bass.wav", "url": "https://..."},
    "other": {"filename": "other.wav", "key": "stems/abc-123/other.wav", "url": "https://..."}
  }
}
```

Failures return `{"error": "..."}`, which RunPod reports as `FAILED`.

### Testing the worker with curl

```bash
JOB_ID=$(curl -s -X POST   -H "Authorization: Bearer $RUNPOD_API_KEY"   -H "Content-Type: application/json"   --data '{"input":{"audio_url":"https://example.com/song.mp3","model_name":"htdemucs_ft"}}'   "https://api.runpod.ai/v2/$RUNPOD_ENDPOINT_ID/run" | jq -r .id)

curl -s -H "Authorization: Bearer $RUNPOD_API_KEY"   "https://api.runpod.ai/v2/$RUNPOD_ENDPOINT_ID/status/$JOB_ID" | jq .
```

The only accepted input is `audio_url`: a publicly reachable `http(s)` link to an MP3. Where it is
hosted doesn't matter, as long as the worker can `GET` it without credentials (a presigned URL is
fine). The worker checks the file header and rejects anything that isn't MP3, and stops downloads
over 50 MB (about 20 minutes at 320 kbps); override that with `MAX_INPUT_BYTES` on the endpoint.
Stems are still returned as WAV.

## RunPod client (`client/`)

The root `pyproject.toml` exposes a `runpod-demucs` script that submits an async job, polls until
the stems are in R2, prints their URLs, and downloads them:

```bash
uv sync
uv run runpod-demucs --audio-url https://example.com/song.mp3 --save-dir stems
uv run runpod-demucs --audio-url https://... --no-wait          # just print the job ID
uv run runpod-demucs --job-id <id>                              # resume polling a job
```

Dynaconf loads `RUNPOD_API_KEY`, `RUNPOD_ENDPOINT_ID`, and `RUNPOD_ENDPOINT_URL` from your
environment or `.env` files (see the [Dynaconf env var docs](https://www.dynaconf.com/envvars/)).
Other knobs: `--model-name`, `--shifts`, `--overlap`, `--poll-interval`, `--max-wait`, and
`--no-download` to only print URLs. `client/runpod_client.py` houses the implementation.

## Next steps

- Bake the Demucs model weights into the Docker image to cut cold-start time.
- Keep `AGENTS.md` in sync with any new workflows so future contributors know which directory to touch.
