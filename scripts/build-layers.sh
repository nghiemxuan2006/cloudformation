#!/usr/bin/env bash
# Build every layer in templates/layers/<name> into build/layers/<name>/python:
#   - pip install requirements.txt (python3.12 / arm64 wheels)
#   - copy the layer's own python/ source
#   - "common" also gets the RDS CA bundle for TLS to Aurora
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

for layer_dir in "$ROOT_DIR"/templates/layers/*/; do
  layer_dir="${layer_dir%/}"
  name="$(basename "$layer_dir")"
  out="$ROOT_DIR/build/layers/$name/python"
  echo ">> Building layer: $name"
  rm -rf "$ROOT_DIR/build/layers/$name"
  mkdir -p "$out"

  if [[ -f "$layer_dir/requirements.txt" ]]; then
    python3 -m pip install \
      --requirement "$layer_dir/requirements.txt" \
      --target "$out" \
      --platform manylinux2014_aarch64 \
      --implementation cp \
      --python-version 3.12 \
      --only-binary=:all: \
      --upgrade --quiet --disable-pip-version-check
  fi

  if [[ -d "$layer_dir/python" ]]; then
    cp -R "$layer_dir/python/." "$out/"
  fi

  if [[ "$name" == "common" ]]; then
    curl -fsSL https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem \
      -o "$out/common/rds-global-bundle.pem"
  fi

  find "$out" -type d -name __pycache__ -prune -exec rm -rf {} +
done
