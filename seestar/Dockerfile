# syntax=docker/dockerfile:1
# Home Assistant "Seestar" app image: the bundled seestar_alp Alpaca driver plus
# our seestar_bridge telemetry publisher, supervised by the base image's
# s6-overlay v3. BUILD_FROM is supplied per-arch from build.yaml (the HA
# base-debian images already ship s6-overlay + bashio).
#
# Debian (glibc) is deliberate: opencv-python publishes no musllinux wheels, so
# an Alpine base forces an hours-long source build, while Debian pulls manylinux
# wheels for every compiled dep (opencv, numpy, pandas, astropy, scikit-image,
# ephem, Pillow) on both amd64 and aarch64 — nothing heavy compiles here. It
# also matches upstream seestar_alp's tested environment (ubuntu:24.04).
ARG BUILD_FROM
FROM ${BUILD_FROM}

# Pinned upstream seestar_alp release. Bumping this is a one-line change plus a
# re-verify of the /config scrape (see plan Task 9).
ARG SEESTAR_ALP_REF=v3.2.2

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Runtime interpreter + the only shared libs the manylinux wheels don't bundle:
# opencv-python needs libGL (libgl1) and libgthread-2.0 (libglib2.0-0t64 on
# trixie). git serves the seestar_alp clone and its pyindi git requirement.
# hadolint ignore=DL3008
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        git \
        python3 \
        python3-pip \
        python3-venv \
        libgl1 \
        libglib2.0-0t64 \
    && rm -rf /var/lib/apt/lists/*

# Debian's system python is PEP 668 externally-managed, so all pip installs go
# into a dedicated venv. Putting its bin dir first on PATH makes `pip` below and
# the plain `python3` in the s6 run scripts resolve to the venv interpreter:
# s6-overlay v3 stashes the container's initial environment (incl. this PATH) in
# /run/s6/container_environment, and both longruns start via with-contenv.
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

# --- seestar_alp (GPL-3.0, bundled at arm's length over the Alpaca socket) -----
# Shallow-clone the pinned tag and install its pinned requirements. The four
# LICENSE-*.txt files live at the repo root and are preserved in the image as-is
# (see /app/seestar_alp/LICENSE*.txt); NOTICE links the upstream project.
RUN git clone --depth 1 --branch "${SEESTAR_ALP_REF}" \
        https://github.com/smart-underworld/seestar_alp /app/seestar_alp \
    && pip install --no-cache-dir -r /app/seestar_alp/requirements.txt

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
# script); seestar_alp runs from /app/seestar_alp. s6-overlay (from the base
# image) is the entrypoint and starts init-config -> {seestar-alp, bridge}.
WORKDIR /app
