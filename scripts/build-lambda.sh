#!/usr/bin/env bash
# Build the Lambda deployment directory at build/lambda (source + deps + RDS CA bundle).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$ROOT_DIR/build/lambda"

rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"

echo ">> Installing dependencies for python3.12 / arm64"
python3 -m pip install \
  --requirement "$ROOT_DIR/src/api/requirements.txt" \
  --target "$BUILD_DIR" \
  --platform manylinux2014_aarch64 \
  --implementation cp \
  --python-version 3.12 \
  --only-binary=:all: \
  --upgrade --quiet

echo ">> Copying source"
cp "$ROOT_DIR"/src/api/*.py "$BUILD_DIR/"

echo ">> Downloading RDS CA bundle (TLS verification for Aurora)"
curl -fsSL https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem \
  -o "$BUILD_DIR/rds-global-bundle.pem"

echo ">> Build ready at $BUILD_DIR"
