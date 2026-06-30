# syntax=docker/dockerfile:1
# Home Assistant "Seestar" app image: the bundled seestar_alp Alpaca driver plus
# our seestar_bridge telemetry publisher, supervised by the base image's
# s6-overlay v3. BUILD_FROM is supplied per-arch from build.yaml (the HA
# base-python:alpine images already ship s6-overlay + bashio).
ARG BUILD_FROM
FROM ${BUILD_FROM}

# Pinned upstream seestar_alp release. Bumping this is a one-line change plus a
# re-verify of the /config scrape (see plan Task 9).
ARG SEESTAR_ALP_REF=v3.2.2

SHELL ["/bin/ash", "-o", "pipefail", "-c"]

# Build toolchain for the compiled wheels seestar_alp pulls (numpy, pandas,
# astropy, scikit-image, opencv, cryptography, pyindi). The -dev headers stay in
# the image as a deliberate tradeoff: a multi-stage slim-down is a follow-up, and
# the runtime shared libs (libstdc++, mesa-gl, glib, freetype, jpeg) are required
# at run time by opencv/Pillow regardless.
# hadolint ignore=DL3018
RUN apk add --no-cache \
        git \
        build-base \
        cmake \
        linux-headers \
        pkgconf \
        python3-dev \
        libffi-dev \
        openssl-dev \
        freetype-dev \
        jpeg-dev \
        zlib-dev \
        libpng-dev \
        openblas-dev \
        lapack-dev \
        gfortran \
        libstdc++ \
        libgl \
        glib \
        mesa-gl

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
