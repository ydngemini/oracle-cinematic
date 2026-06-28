#!/usr/bin/env bash
# Neoh reconstruction job — runs inside the AWS Batch GPU container.
# Reads capture images from INPUT_S3, runs COLMAP poses + 3D Gaussian Splatting
# (nerfstudio splatfacto, Apache-2.0), converts the .ply to the antimatter15
# .splat the gsplat web viewer loads, and uploads it to OUTPUT_S3.
set -euo pipefail

: "${INPUT_S3:?INPUT_S3 (s3://bucket/recon-inputs/<job>) is required}"
: "${OUTPUT_S3:?OUTPUT_S3 (s3://bucket/recon-outputs/<job>/model.splat) is required}"
ITERS="${RECON_ITERS:-7000}"

WORK=/work
rm -rf "$WORK"; mkdir -p "$WORK/images" "$WORK/proc" "$WORK/out" "$WORK/export"

echo ">> pulling capture from $INPUT_S3"
aws s3 cp --recursive "$INPUT_S3" "$WORK/images"
COUNT=$(find "$WORK/images" -type f | wc -l)
echo ">> $COUNT source images"
[ "$COUNT" -ge 8 ] || { echo "!! need >=8 images for reconstruction, got $COUNT" >&2; exit 2; }

echo ">> [1/4] COLMAP poses (ns-process-data)"
ns-process-data images --data "$WORK/images" --output-dir "$WORK/proc" --verbose

echo ">> [2/4] train splatfacto ($ITERS iters)"
ns-train splatfacto --data "$WORK/proc" --output-dir "$WORK/out" \
  --max-num-iterations "$ITERS" --viewer.quit-on-train-completion True

CONFIG=$(find "$WORK/out" -name config.yml | head -1)
[ -n "$CONFIG" ] || { echo "!! no trained config produced" >&2; exit 3; }

echo ">> [3/4] export gaussian splat (.ply)"
ns-export gaussian-splat --load-config "$CONFIG" --output-dir "$WORK/export"
PLY=$(find "$WORK/export" -name '*.ply' | head -1)
[ -n "$PLY" ] || { echo "!! no .ply exported" >&2; exit 4; }

echo ">> [4/4] .ply -> .splat (PlayCanvas splat-transform, MIT)"
splat-transform "$PLY" "$WORK/model.splat"

echo ">> uploading to $OUTPUT_S3"
aws s3 cp "$WORK/model.splat" "$OUTPUT_S3" --content-type application/octet-stream
echo ">> done"
