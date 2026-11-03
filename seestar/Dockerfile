# syntax=docker/dockerfile:1
# Home Assistant "Seestar" app image: the seestar_bridge telemetry publisher,
# supervised by the base image's s6-overlay v3. BUILD_FROM is supplied per-arch
# from build.yaml (the HA base-debian images already ship s6-overlay + bashio).
# The seestar_alp driver is not in this image: it runs as its own add-on
# (https://github.com/jaxzin/ha_seestar_alp) or wherever the operator runs it.
ARG BUILD_FROM
FROM ${BUILD_FROM}

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# hadolint ignore=DL3008
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        python3 \
        python3-venv \
    && rm -rf /var/lib/apt/lists/*

# Debian's system python is PEP 668 externally-managed, so all pip installs go
# into a dedicated venv. Putting its bin dir first on PATH makes `pip` below and
# the plain `python3` in the s6 run script resolve to the venv interpreter:
# s6-overlay v3 stashes the container's initial environment (incl. this PATH) in
# /run/s6/container_environment, and the longrun starts via with-contenv.
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

# --- seestar_bridge (our code, Apache-2.0) ------------------------------------
# Install the bridge's pinned, hashed deps first (better layer caching), then the
# package source. requirements.txt is the `uv export` hashed lock, so install it
# with --require-hashes for supply-chain integrity.
COPY bridge/requirements.txt /app/bridge/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /app/bridge/requirements.txt
COPY bridge/ /app/bridge/

# --- s6 services + init (the rootfs overlay) ----------------------------------
COPY rootfs /

# The bridge runs in-place from /app/bridge via PYTHONPATH (set in its s6 run
# script). s6-overlay (from the base image) is the entrypoint and starts
# init-config -> bridge.
WORKDIR /app
