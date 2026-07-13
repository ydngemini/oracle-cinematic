#!/usr/bin/env bash
# Transport-free reconstruction core: <images_dir> -> <out_splat>.
# COLMAP poses (ns-process-data) -> train splatfacto (Apache-2.0 3DGS) ->
# export .ply -> PlayCanvas splat-transform -> antimatter15 .splat.
#
# Same recipe as the AWS Batch worker (infra/reconstruction/run.sh) with the S3
# I/O stripped out — handler.py owns the transport (S3 / presigned URL / base64).
set -euo pipefail

IMAGES_DIR="${1:?usage: pipeline.sh <images_dir> <out_splat>}"
OUT_SPLAT="${2:?usage: pipeline.sh <images_dir> <out_splat>}"
ITERS="${RECON_ITERS:-7000}"

WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT
mkdir -p "$WORK/proc" "$WORK/out" "$WORK/export"

COUNT=$(find "$IMAGES_DIR" -type f | wc -l)
echo ">> $COUNT source images"
[ "$COUNT" -ge 8 ] || { echo "!! need >=8 images for reconstruction, got $COUNT" >&2; exit 2; }

echo ">> [1/4] COLMAP poses (ns-process-data)"
ns-process-data images --data "$IMAGES_DIR" --output-dir "$WORK/proc" --verbose

echo ">> [2/4] train splatfacto ($ITERS iters)"
ns-train splatfacto --data "$WORK/proc" --output-dir "$WORK/out" \
  --max-num-iterations "$ITERS" --viewer.quit-on-train-completion True

CONFIG=$(find "$WORK/out" -name config.yml -print -quit)
[ -n "$CONFIG" ] || { echo "!! no trained config produced" >&2; exit 3; }

echo ">> [3/4] export gaussian splat (.ply)"
ns-export gaussian-splat --load-config "$CONFIG" --output-dir "$WORK/export"
PLY=$(find "$WORK/export" -name '*.ply' -print -quit)
[ -n "$PLY" ] || { echo "!! no .ply exported" >&2; exit 4; }

echo ">> [4/4] .ply -> .splat (PlayCanvas splat-transform, MIT)"
splat-transform "$PLY" "$OUT_SPLAT"
echo ">> wrote $OUT_SPLAT"
