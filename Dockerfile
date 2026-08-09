# syntax=docker/dockerfile:1
#
# Vectra-180 in a container.
#
# This is for development, CI and desk testing on an x86 box. The supported way
# to run it on a Raspberry Pi is the systemd service in deploy/ -- a dashcam
# wants to come up on ignition with the shortest possible path between the
# camera and the disk, and a container runtime is not on that path.

# --- build the wheel -------------------------------------------------------

FROM python:3.12-slim-bookworm AS builder

WORKDIR /build
RUN pip install --no-cache-dir uv==0.5.11

# hatchling reads the version out of the package and the description out of the
# README, so both have to be present for the build to succeed.
COPY pyproject.toml README.md LICENSE.md ./
COPY src/ ./src/

RUN uv build --wheel --out-dir /dist

# --- runtime ---------------------------------------------------------------

FROM python:3.12-slim-bookworm AS runtime

LABEL org.opencontainers.image.title="Vectra-180" \
      org.opencontainers.image.description="Dual-fisheye dashcam and stereoscopic depth engine" \
      org.opencontainers.image.source="https://github.com/Life-Experimentalist/Vectra-180" \
      org.opencontainers.image.licenses="Apache-2.0"

# ffmpeg      -- the preferred encoder; without it the OpenCV writer is used
# libgl1      -- OpenCV links libGL even in a headless build
# libglib2.0-0 -- and libglib
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl

# Non-root, and in `video` so a passed-through /dev/video* is readable. Group 44
# is Debian's video gid, which is also what Raspberry Pi OS uses -- so the same
# image works against a host device without remapping.
RUN useradd --create-home --uid 1000 --gid video vectra \
    && mkdir -p /recordings \
    && chown vectra:video /recordings

ENV VECTRA_RECORDING_DIR=/recordings \
    PYTHONUNBUFFERED=1 \
    OPENCV_NUM_THREADS=2

# Inside a container 0.0.0.0 means "this container's network namespace", not
# "the internet" -- publishing the port is the operator's separate, explicit
# act. Set VECTRA_SERVER_TOKEN when you do publish it: without one, anybody who
# can reach the port can download and delete the footage.
ENV VECTRA_SERVER_HOST=0.0.0.0

VOLUME ["/recordings"]
EXPOSE 8080
USER vectra
WORKDIR /home/vectra

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request as r; r.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"

ENTRYPOINT ["vectra180"]
CMD ["run"]
