# syntax=docker/dockerfile:1

# Two stages so the runtime image never carries a compiler or a wheel cache. The
# dependencies are resolved once here into a throwaway prefix and copied across.

FROM python:3.12-slim AS builder

WORKDIR /build
COPY requirements.txt .

# --prefix keeps everything under /install so the result can be copied wholesale.
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


FROM python:3.12-slim

# opencv-python-headless is used deliberately, so no X11 or libGL stack is installed.
# libgomp1 is the one runtime OpenCV and NumPy genuinely need for their thread pools.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Non-root, with a fixed uid so ownership of a mounted volume is predictable.
RUN useradd --create-home --uid 10001 appuser

WORKDIR /app

COPY --from=builder /install /usr/local
COPY src/ ./src/
COPY config/ ./config/

# tools/ is deliberately absent: the synthetic feed generator is fixture tooling, and
# the pipeline consumes a feed rather than producing one. The video is mounted at run
# time (see docker-compose.yml), which keeps the image free of generated artefacts.
ENV PYTHONPATH=/app/src \
    # Unbuffered, so log lines reach the orchestrator as they happen rather than when
    # the process ends. For a batch job that is the difference between monitoring a run
    # and reading its post-mortem.
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER appuser

# The same CLI a developer runs locally. Nothing about the entry point is
# container-specific, so a failure here reproduces outside Docker and vice versa.
ENTRYPOINT ["python", "-m", "trackbox_pitch.cli"]
CMD ["--config", "config/default.yaml"]
