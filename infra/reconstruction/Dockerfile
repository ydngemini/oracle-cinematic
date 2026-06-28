# Neoh reconstruction GPU job image (AWS Batch, SPOT g5).
# COLMAP + nerfstudio splatfacto (Apache-2.0, commercial-OK) + PlayCanvas
# splat-transform (.ply→.splat, MIT) + awscli. Reads INPUT_S3 → writes OUTPUT_S3.
#
# Build on a machine with the NVIDIA toolchain (or CodeBuild GPU); the tiny-cuda-nn
# build that nerfstudio pulls needs nvcc from this -devel base. Pin versions if a
# future nerfstudio/torch release drifts. Push to the ECR repo created by
# infra/terraform/reconstruction.tf, then set that image as var.recon_image_tag.
FROM nvidia/cuda:12.1.1-devel-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    TORCH_CUDA_ARCH_LIST="8.6" \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
      git curl ca-certificates build-essential \
      python3 python3-pip python3-dev \
      colmap ffmpeg \
      nodejs npm \
    && rm -rf /var/lib/apt/lists/*

# .ply -> .splat converter (MIT) + awscli for S3 I/O.
RUN npm install -g @playcanvas/splat-transform \
    && pip3 install --upgrade pip awscli

# Torch (CUDA 12.1) then nerfstudio (pulls tinycudann/gsplat — Apache-2.0).
RUN pip3 install torch==2.1.2 torchvision==0.16.2 --index-url https://download.pytorch.org/whl/cu121
RUN pip3 install nerfstudio

COPY run.sh /usr/local/bin/run.sh
RUN chmod +x /usr/local/bin/run.sh

ENTRYPOINT ["/usr/local/bin/run.sh"]
