#!/usr/bin/env bash
# Package and deploy the root stack for one environment.
#
# Usage: scripts/deploy.sh <dev|test|prod> [--plan]
#   --plan   create the change set and print it, but do not execute it
#
# Required env vars:
#   ARTIFACT_BUCKET         S3 bucket from the bootstrap stack
# Optional env vars:
#   AWS_REGION              defaults to the CLI's configured region
#   CFN_EXECUTION_ROLE_ARN  role CloudFormation assumes (recommended in CI)
#   SKIP_BUILD=1            reuse an existing build/lambda directory
set -euo pipefail

ENVIRONMENT="${1:?Usage: $0 <dev|test|prod> [--plan]}"
MODE="${2:-}"
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PARAM_FILE="$ROOT_DIR/infra/parameters/${ENVIRONMENT}.json"
BUILD_ROOT="$ROOT_DIR/build"

: "${ARTIFACT_BUCKET:?ARTIFACT_BUCKET must be set}"
AWS_REGION="${AWS_REGION:-$(aws configure get region)}"
[[ -f "$PARAM_FILE" ]] || { echo "Parameter file not found: $PARAM_FILE" >&2; exit 1; }

FILE_ENV="$(jq -r '.Parameters.Environment' "$PARAM_FILE")"
if [[ "$FILE_ENV" != "$ENVIRONMENT" ]]; then
  echo "Environment mismatch: argument=$ENVIRONMENT, $PARAM_FILE says $FILE_ENV" >&2
  exit 1
fi

PROJECT_NAME="$(jq -r '.Parameters.ProjectName' "$PARAM_FILE")"
STACK_NAME="${PROJECT_NAME}-${ENVIRONMENT}"
GIT_SHA="$(git -C "$ROOT_DIR" rev-parse --short HEAD 2>/dev/null || echo local)"
PACKAGED_TEMPLATE="$BUILD_ROOT/packaged-${ENVIRONMENT}.yaml"

params=()
while IFS= read -r line; do params+=("$line"); done \
  < <(jq -r '.Parameters | to_entries[] | "\(.key)=\(.value)"' "$PARAM_FILE")
tags=()
while IFS= read -r line; do tags+=("$line"); done \
  < <(jq -r '.Tags | to_entries[] | "\(.key)=\(.value)"' "$PARAM_FILE")

if [[ "${SKIP_BUILD:-0}" != "1" ]]; then
  "$ROOT_DIR/scripts/build-lambda.sh"
fi

echo ">> Packaging templates -> s3://$ARTIFACT_BUCKET/$STACK_NAME/$GIT_SHA"
aws cloudformation package \
  --region "$AWS_REGION" \
  --template-file "$ROOT_DIR/infra/main.yaml" \
  --s3-bucket "$ARTIFACT_BUCKET" \
  --s3-prefix "$STACK_NAME/$GIT_SHA" \
  --output-template-file "$PACKAGED_TEMPLATE"

deploy_args=(
  --region "$AWS_REGION"
  --template-file "$PACKAGED_TEMPLATE"
  --stack-name "$STACK_NAME"
  --s3-bucket "$ARTIFACT_BUCKET"
  --s3-prefix "$STACK_NAME/$GIT_SHA"
  --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND
  --parameter-overrides "${params[@]}"
  --tags "${tags[@]}"
  --no-fail-on-empty-changeset
)
if [[ -n "${CFN_EXECUTION_ROLE_ARN:-}" ]]; then
  deploy_args+=(--role-arn "$CFN_EXECUTION_ROLE_ARN")
fi
if [[ "$MODE" == "--plan" ]]; then
  deploy_args+=(--no-execute-changeset)
fi

echo ">> Deploying stack $STACK_NAME ($AWS_REGION)"
aws cloudformation deploy "${deploy_args[@]}"

if [[ "$MODE" != "--plan" ]]; then
  echo ">> Stack outputs"
  aws cloudformation describe-stacks \
    --region "$AWS_REGION" \
    --stack-name "$STACK_NAME" \
    --query 'Stacks[0].Outputs' \
    --output table
fi
