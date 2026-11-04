#!/usr/bin/env bash
# Deploy one environment, stack by stack, following the templates/ layout.
#
# Usage: scripts/deploy.sh <dev|test|prod> [all|infrastructure|layers|functions|api] [stack]
#   stack: only with "infrastructure" - deploy one stack, e.g. `database`
#          (= templates/infrastructure/database.yml). Used by the per-stack pipelines.
#
# Order (later stacks !ImportValue outputs of earlier ones):
#   1. infrastructure : private-vpc -> storage -> queue -> database -> cache -> secrets -> role -> api-gateway
#   2. layers         : templates/layers/<name>/layer.yml
#   3. functions      : for each templates/lambda-functions/<name>/ (non-API Lambdas, e.g. SQS workers)
#                         template.yml         (function + role)
#                         lambda_function.yml  (version + "live" alias + trigger)
#   4. api            : for each templates/api-gateways/<api>/
#                         main_template.yml                (API resources)
#                         <path>/<METHOD>/template.yml       (function + method)
#                         <path>/<METHOD>/lambda_function.yml (version + "live" alias + permission)
#                       then a new API deployment + stage.yml
#
# Stack names:  <infra>-<env>-<component>    e.g. myapp-dev-fn-items-id-get
#
# Required env: ARTIFACT_BUCKET
# Optional env: AWS_REGION, CFN_EXECUTION_ROLE_ARN, COMMIT_HASH,
#               SKIP_LAYER_BUILD=1 (reuse an existing build/layers)
set -euo pipefail

ENV_TYPE="${1:?Usage: $0 <dev|test|prod> [all|infrastructure|layers|functions|api] [stack]}"
SCOPE="${2:-all}"
STACK="${3:-}"
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATES="$ROOT_DIR/templates"
PARAM_FILE="$ROOT_DIR/parameters/${ENV_TYPE}.json"

: "${ARTIFACT_BUCKET:?ARTIFACT_BUCKET must be set}"
AWS_REGION="${AWS_REGION:-$(aws configure get region)}"
export AWS_REGION
[[ -f "$PARAM_FILE" ]] || { echo "Parameter file not found: $PARAM_FILE" >&2; exit 1; }
if [[ "$(jq -r '.Parameters.EnvironmentType' "$PARAM_FILE")" != "$ENV_TYPE" ]]; then
  echo "EnvironmentType in $PARAM_FILE does not match '$ENV_TYPE'" >&2
  exit 1
fi

INFRA="$(jq -r '.Parameters.InfrastructureName' "$PARAM_FILE")"
PREFIX="$INFRA-$ENV_TYPE"
COMMIT_HASH="${COMMIT_HASH:-$(git -C "$ROOT_DIR" rev-parse --short HEAD 2>/dev/null || echo local)}"
PACKAGED_DIR="$ROOT_DIR/build/packaged/$ENV_TYPE"
mkdir -p "$PACKAGED_DIR"

# Every stack receives the whole parameter file; `aws cloudformation deploy`
# only forwards the keys a template actually declares.
env_params=()
while IFS= read -r line; do env_params+=("$line"); done \
  < <(jq -r '.Parameters | to_entries[] | "\(.key)=\(.value)"' "$PARAM_FILE")
tags=()
while IFS= read -r line; do tags+=("$line"); done \
  < <(jq -r '.Tags | to_entries[] | "\(.key)=\(.value)"' "$PARAM_FILE")

# deploy_stack <template> <component> [Key=Value ...]
deploy_stack() {
  local template="$1" component="$2"
  shift 2
  local stack="$PREFIX-$component"
  local packaged="$PACKAGED_DIR/$component.yml"

  # Local test runs leave bytecode in src/ - keep it out of the Lambda zip.
  find "$(dirname "$template")" -name __pycache__ -type d -prune -exec rm -rf {} +

  echo ""
  echo "==> $stack  (${template#"$ROOT_DIR/"})"
  aws cloudformation package \
    --template-file "$template" \
    --s3-bucket "$ARTIFACT_BUCKET" \
    --s3-prefix "$PREFIX/$component" \
    --output-template-file "$packaged" > /dev/null

  local args=(
    --template-file "$packaged"
    --stack-name "$stack"
    --s3-bucket "$ARTIFACT_BUCKET"
    --s3-prefix "$PREFIX/$component"
    --capabilities CAPABILITY_NAMED_IAM
    --parameter-overrides "${env_params[@]}" "$@"
    --tags "${tags[@]}"
    --no-fail-on-empty-changeset
  )
  if [[ -n "${CFN_EXECUTION_ROLE_ARN:-}" ]]; then
    args+=(--role-arn "$CFN_EXECUTION_ROLE_ARN")
  fi
  aws cloudformation deploy "${args[@]}"
}

# stack_output <component> <OutputKey>
stack_output() {
  aws cloudformation describe-stacks \
    --stack-name "$PREFIX-$1" \
    --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" \
    --output text
}

INFRA_STACKS=(private-vpc storage queue database cache secrets role api-gateway)

deploy_infrastructure() {
  local dir="$TEMPLATES/infrastructure"
  if [[ -n "$STACK" ]]; then
    [[ -f "$dir/$STACK.yml" ]] || {
      echo "Unknown infrastructure stack '$STACK' (expected one of: ${INFRA_STACKS[*]})" >&2
      exit 1
    }
    deploy_stack "$dir/$STACK.yml" "$STACK"
    return
  fi
  for component in "${INFRA_STACKS[@]}"; do
    deploy_stack "$dir/$component.yml" "$component"
  done
}

deploy_layers() {
  if [[ "${SKIP_LAYER_BUILD:-0}" != "1" ]]; then
    "$ROOT_DIR/scripts/build-layers.sh"
  fi
  for layer_dir in "$TEMPLATES"/layers/*/; do
    layer_dir="${layer_dir%/}"
    local name
    name="$(basename "$layer_dir")"
    deploy_stack "$layer_dir/layer.yml" "layer-$name"
  done
}

# templates/lambda-functions/process-job -> function/stack "fn-process-job"
deploy_functions() {
  local layer_arn
  layer_arn="$(stack_output layer-common LayerVersionArn)"
  for fn_dir in "$TEMPLATES"/lambda-functions/*/; do
    fn_dir="${fn_dir%/}"
    [[ -f "$fn_dir/template.yml" ]] || continue
    local name
    name="$(basename "$fn_dir")"
    deploy_stack "$fn_dir/template.yml" "fn-$name" \
      "LambdaFunctionFileName=$name" "CommonLayerArn=$layer_arn"
    deploy_stack "$fn_dir/lambda_function.yml" "fn-$name-alias" \
      "LambdaFunctionFileName=$name" "CommitHash=$COMMIT_HASH"
  done
}

lower() { tr '[:upper:]' '[:lower:]'; }

# templates/api-gateways/items/{id}/GET
#   resource key : items-id        (matches main_template export ApiResource-items-id)
#   function key : items-id-get    (function, role, log group and stack names)
#   api path     : items/*         (invoke permission source ARN)
deploy_api() {
  local layer_arn
  layer_arn="$(stack_output layer-common LayerVersionArn)"

  for api_dir in "$TEMPLATES"/api-gateways/*/; do
    api_dir="${api_dir%/}"
    [[ -f "$api_dir/main_template.yml" ]] || continue
    local api
    api="$(basename "$api_dir")"
    deploy_stack "$api_dir/main_template.yml" "api-$api"

    while IFS= read -r template; do
      local method_dir rel method resource_rel resource_key fn_key api_path
      method_dir="$(dirname "$template")"
      rel="${method_dir#"$TEMPLATES/api-gateways/"}"
      method="$(basename "$rel" | tr '[:lower:]' '[:upper:]')"
      resource_rel="$(dirname "$rel")"
      resource_key="$(echo "$resource_rel" | tr -d '{}' | tr '/' '-' | lower)"
      fn_key="$resource_key-$(echo "$method" | lower)"
      api_path="$(echo "$resource_rel" | sed -E 's/\{[^}]+\}/*/g')"

      local fn_params=(
        "LambdaFunctionFileName=$fn_key"
        "ResourceKey=$resource_key"
        "HttpMethod=$method"
      )
      deploy_stack "$template" "fn-$fn_key" "${fn_params[@]}" "CommonLayerArn=$layer_arn"
      deploy_stack "$method_dir/lambda_function.yml" "fn-$fn_key-alias" "${fn_params[@]}" \
        "ApiPath=$api_path" "CommitHash=$COMMIT_HASH"
    done < <(find "$api_dir" -mindepth 2 -name template.yml | sort)
  done

  echo ""
  echo "==> Creating API deployment"
  local rest_api_id deployment_id
  rest_api_id="$(stack_output api-gateway RestApiId)"
  deployment_id="$(aws apigateway create-deployment \
    --rest-api-id "$rest_api_id" \
    --description "$COMMIT_HASH" \
    --query id --output text)"
  deploy_stack "$TEMPLATES/api-gateways/stage.yml" "api-stage" "DeploymentId=$deployment_id"

  echo ""
  echo "API URL: $(stack_output api-stage ApiUrl)"
}

if [[ -n "$STACK" && "$SCOPE" != "infrastructure" ]]; then
  echo "A stack name is only valid with scope 'infrastructure'" >&2
  exit 1
fi

case "$SCOPE" in
  infrastructure) deploy_infrastructure ;;
  layers)         deploy_layers ;;
  functions)      deploy_functions ;;
  api)            deploy_api ;;
  all)            deploy_infrastructure; deploy_layers; deploy_functions; deploy_api ;;
  *) echo "Unknown scope: $SCOPE" >&2; exit 1 ;;
esac
