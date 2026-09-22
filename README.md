# Pitch Boundary & Camera Crop Engine

This reads a match video, finds the pitch boundary in each frame, works out a result,
and tells the platform how the run went.

It replaces the old research prototype. That prototype is still in `legacy/`, untouched,
so you can compare. See `DECISIONS.md` for why things are the way they are.

## What you need

- Python 3.11 or newer (the container uses 3.12)
- Docker if you want to run it the way it would run in production
- A video. There is a script that makes a fake one for testing.

## Running it locally

```bash
python -m venv .venv
.venv/Scripts/activate          # on Windows. On Mac or Linux use: source .venv/bin/activate
pip install -r requirements.txt

# Make a fake video first. Videos aren't committed to git.
python -c "from tools.synthetic_generator import generate_synthetic_video; generate_synthetic_video()"

# Run it. --no-reporting means don't try to talk to the platform.
python -m trackbox_pitch.cli --no-reporting --log-format console
```

Logs are JSON by default, because in production nobody is watching the screen. If you
are a human reading them, add `--log-format console`.

## Running it in Docker

```bash
python -c "from tools.synthetic_generator import generate_synthetic_video; generate_synthetic_video()"
docker compose up --build
```

This starts the reporting service first and waits until it is actually ready, then runs
the pipeline. The pipeline talks to it over the network. No shared files, no database.

To see what the platform received:

```bash
curl -s http://localhost:5000/api/v1/jobs/events | jq
```

The video is mounted into the container instead of being copied into the image, so
generated files never end up baked into an image.

## Settings

`config/default.yaml` holds everything. Every field has to be there. There are no hidden
defaults.

If something is missing, misspelled, or out of range, the program stops immediately and
tells you which key was wrong. It does not run halfway and then fall over, and it does
not quietly use a value you did not choose.

You can also override settings with environment variables. They go through the same
checks as the file, so you cannot sneak in a bad value that way either.

| Variable | Changes |
|---|---|
| `TRACKBOX_VIDEO_PATH` | `video.path` |
| `TRACKBOX_TARGET_FPS` | `video.target_fps` |
| `TRACKBOX_API_URL` | `reporting.api_url` |
| `TRACKBOX_LOG_LEVEL` | `logging.level` |
| `TRACKBOX_LOG_FORMAT` | `logging.format` |

The file wins over nothing, then environment variables, then command line flags.

```bash
python -m trackbox_pitch.cli --help
```

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Finished fine |
| 1 | Something unexpected broke. A bug. |
| 2 | The config is wrong |
| 3 | The video is missing, unreadable, or empty |
| 4 | The video is cut short or a frame would not decode |
| 5 | Ran fine, but found no pitch boundary anywhere |

They are all different on purpose. A scheduler should be able to tell "bad config" from
"bad video" from "ran fine but saw nothing" without reading any log text.

## What it does

- Keeps the finding of boundaries separate from the running of things. The pipeline asks
  for a `FieldDetector` and does not know which one it gets. Detectors are picked by name
  from the config, so a typo in a detector name fails at startup.
- Only looks at frames it needs. A 1800 frame video gets looked at 300 times, not 1800.
  Frames it skips are never decoded, just stepped over.
- Does not keep anything that grows with the video length. Polygons are kept in a small
  ring buffer.
- Splits failures into "stop now" and "keep going", and counts every rejected frame with
  the reason. Nothing gets dropped without being counted.
- Sends reports built from proper models, and treats an unreachable platform as a
  degradation, not a failure. But it never lets that hide a real failure.

## What it does not do

- No crop layout recommendation. The old prototype had a config block for it but never
  read it. I left that block out instead of copying decoration.
- The detector is not tuned. `green_threshold` is a straight port, known limitations and
  all. See `DECISIONS.md`.
- If the video resolution changes halfway through, it stops. Mixing up two coordinate
  systems would quietly ruin every number, so stopping is better.

## Tests

```bash
python -m pytest -q
python -m ruff check src/ tests/
```

## Where things live

```
src/trackbox_pitch/
  config.py         settings, and the checks that reject bad ones
  errors.py         what can go wrong, and what exit code it means
  models.py         the shapes that get passed around and sent to the platform
  detectors/        the one part that swaps out: finding the boundary
  video.py          deciding which frames to look at
  aggregation.py    adding up the results
  logging_setup.py  one JSON line per event
  reporting.py      talking to the platform
  pipeline.py       runs the whole job
  cli.py            the command line entry point
tools/              makes the fake video. Not part of the shipped code.
legacy/             the old prototype, untouched
config/             default.yaml
tests/              the tests
docs/               design notes
mock_api/           the reporting service we were given. Not modified.
```
