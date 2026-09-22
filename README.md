# Automated Pitch Boundary & Camera Crop Engine

A production pipeline that ingests match video, detects the playing-field boundary in
each frame, aggregates a result, and reports progress and outcome to the platform over
HTTP.

It replaces the v0.1 research prototype, which is kept unmodified in `legacy/` for
reference. What changed and why is in [`DECISIONS.md`](DECISIONS.md); the design
reasoning is in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## Requirements

- Python 3.11+ (the container uses 3.12)
- Docker, for the containerised path
- A video feed. The synthetic one is produced by `tools/synthetic_generator.py`

---

## Quick start

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows;  source .venv/bin/activate on POSIX
pip install -r requirements.txt

# The feed is generated tooling output and is gitignored, so make one first.
python -c "from tools.synthetic_generator import generate_synthetic_video; generate_synthetic_video()"

# Run with reporting switched off, for a local run with no platform listening.
python -m trackbox_pitch.cli --no-reporting --log-format console
```

Machine-readable output is the default, because the consumer is an orchestrator rather
than a person:

```bash
python -m trackbox_pitch.cli --no-reporting | jq -c 'select(.event) | {event, level}'
```

---

## Running in Docker

This is the deployment path. The pipeline runs in a container and reports to the
reporting service over the network — no shared file, no database.

```bash
python -c "from tools.synthetic_generator import generate_synthetic_video; generate_synthetic_video()"
docker compose up --build
```

`mock_api` starts first and the `runner` waits for it to report healthy. The feed is
mounted read-only at `/data` rather than baked into the image.

Inspect what the platform received:

```bash
curl -s http://localhost:5000/api/v1/jobs/events | jq
```

---

## Configuration

`config/default.yaml` is the single source of truth. **Every field is required** — there
are no built-in defaults to fall back on. A missing, misspelled, or out-of-range value
stops the run at startup with a message naming the key, rather than failing part-way
through or silently running with a value nobody chose.

Overrides are validated by exactly the same models as the file, so an override cannot
set something the file would have been rejected for.

| Environment variable | Overrides |
|---|---|
| `TRACKBOX_VIDEO_PATH` | `video.path` |
| `TRACKBOX_TARGET_FPS` | `video.target_fps` |
| `TRACKBOX_API_URL` | `reporting.api_url` |
| `TRACKBOX_LOG_LEVEL` | `logging.level` |
| `TRACKBOX_LOG_FORMAT` | `logging.format` |

Precedence: file, then environment, then command-line flags.

```bash
python -m trackbox_pitch.cli --help
```

---

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Completed successfully |
| 1 | Unexpected internal error |
| 2 | Configuration error |
| 3 | Input error (missing, unreadable, or empty video) |
| 4 | Stream error (truncated or undecodable feed) |
| 5 | Completed, but no valid boundary was found |

Deliberately distinct so an orchestrator can tell "bad config" from "bad video" from
"ran fine but saw no pitch" without parsing log text.

---

## What it does and does not do

**Does**

- Splits detection from orchestration: the pipeline imports a `FieldDetector`, never a
  concrete one. Detectors are resolved by name from configuration, so an unknown
  detector is a startup failure.
- Scales work with what needs inspecting, not with file length. Skipped frames are
  demand-ed minimally and never decoded; a 1800-frame feed is analysed in 300 samples.
- Keeps nothing that grows with feed length. Polygons are bounded by a ring buffer.
- Distinguishes fatal from recoverable failures, and counts every rejection with a
  reason instead of dropping it silently.
- Reports to the platform from validated models, and treats an unreachable platform as
  a degradation rather than a failure — while never letting that mask a real failure.

**Does not**

- Implement crop-layout recommendation. The prototype declared a `crop_search` config
  block for it but never read it; that block is deliberately absent here rather than
  carried forward as decoration. See `DECISIONS.md`.
- Calibrate the detector. `green_threshold` is a faithful port, including its known
  limitation.
- Handle a mid-stream resolution change. It stops the run instead, because mixing
  coordinate spaces would silently invalidate every metric.

---

## Testing

```bash
python -m pytest -q
python -m ruff check src/ tests/
```

Stream failure paths use a fake capture rather than a hand-broken video file, so they
test the logic rather than the codec.

---

## Layout

```
src/trackbox_pitch/
  config.py         validated configuration, fail-fast loader
  errors.py         failure taxonomy and exit codes
  models.py         shapes crossing a boundary, internal and wire
  detectors/        the one seam: a detector protocol plus implementations
  video.py          frame scheduling: which frames are worth decoding
  aggregation.py    robust, bounded aggregation of detections
  logging_setup.py  one JSON object per line
  reporting.py      platform reporting, retries, circuit breaker
  pipeline.py       orchestration and failure policy
  cli.py            thin entry point
tools/              feed generator (fixture code, not shipped)
legacy/             the original prototype, unmodified
config/             default.yaml
tests/              the suite
docs/               architecture notes
mock_api/           provided reporting service (untouched)
```
