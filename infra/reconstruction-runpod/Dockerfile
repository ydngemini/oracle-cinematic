# Neoh reconstruction — RunPod Serverless GPU worker.
# COLMAP + nerfstudio splatfacto + PlayCanvas splat-transform, driven by handler.py.
#
# Base is CUDA 12.1 **runtime** (not devel): the build compiles only two tiny C
# wheels (pyliblzfse, fpsample) with gcc — nothing needs nvcc (tinycudann is not
# installed; gsplat/nerfacc ship prebuilt CUDA wheels that use this base's runtime
# libs). Dropping the devel toolkit takes the pushed image from ~7.2 GB to ~2.8 GB
# so RunPod serverless workers finish the cold-start pull before they're preempted
# (the 7.2 GB devel image never left "initializing" on contended GPUs).
FROM nvidia/cuda:12.1.1-runtime-ubuntu22.04

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1

# Distribution package revisions are intentionally resolved from the pinned
# Ubuntu 22.04 base repositories so security rebuilds receive patched packages.
# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends \
      git curl ca-certificates build-essential \
      python3 python3-pip python3-dev \
      colmap ffmpeg \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*
# NOTE: Ubuntu's apt `nodejs` is too old for splat-transform 3.x. NodeSource
# supplies Node 22, the converter's declared minimum runtime.

# .ply -> .splat converter (MIT).
RUN npm install -g @playcanvas/splat-transform@3.0.0 && npm cache clean --force \
    && pip3 install --upgrade pip==26.1.2

# Torch (CUDA 12.1) then nerfstudio. gsplat/nerfacc install as prebuilt wheels and
# use the CUDA runtime libs in this base — no nvcc/devel toolkit required.
RUN pip3 install torch==2.1.2 torchvision==0.16.2 --index-url https://download.pytorch.org/whl/cu121 \
    && pip3 install nerfstudio==1.1.5

# RunPod SDK + presigned-URL transport dependency.
COPY requirements.txt /opt/requirements.txt
RUN pip3 install -r /opt/requirements.txt

RUN groupadd --gid 10001 recon \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin recon \
    && mkdir -p /work \
    && chown -R recon:recon /work
COPY pipeline.sh /usr/local/bin/pipeline.sh
RUN chmod +x /usr/local/bin/pipeline.sh

# Handler at container root + CMD running it from root — the convention the RunPod
# Hub validator resolves through the Dockerfile to confirm runpod.serverless.start().
WORKDIR /
COPY handler.py /handler.py

# RunPod serverless workers start by running the handler; it blocks on the queue.
ENV HOME=/home/recon
USER 10001:10001
CMD ["python3", "-u", "/handler.py"]
