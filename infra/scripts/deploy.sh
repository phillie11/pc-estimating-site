#!/usr/bin/env bash
# Build, push and roll out Siteline to AWS. Linux/macOS/CI version of deploy.ps1 (same steps).
#
#   infra/scripts/deploy.sh staging
#   infra/scripts/deploy.sh prod
#   infra/scripts/deploy.sh prod --image-tag 1a2b3c4d5e6f   # redeploy an image already in ECR (rollback)
#
# Steps: build the image -> push to ECR -> register a new "migrate" task definition and run it ->
# record the image tag -> register the other task definitions -> update the web and worker services
# -> wait until they are stable. Needs: aws (v2), docker, git, jq.
set -euo pipefail

usage() { echo "Usage: $0 staging|prod [--image-tag TAG]" >&2; exit 2; }
die() { echo "ERROR: $*" >&2; exit 1; }
say() { echo "==> $*" >&2; }

ENV_NAME="${1:-}"
[[ "$ENV_NAME" == staging || "$ENV_NAME" == prod ]] || usage
shift
IMAGE_TAG=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --image-tag) IMAGE_TAG="${2:-}"; [[ -n "$IMAGE_TAG" ]] || usage; shift 2 ;;
    *) usage ;;
  esac
done

for tool in aws docker git jq; do command -v "$tool" >/dev/null || die "$tool is not installed"; done
export AWS_REGION=eu-west-2 AWS_DEFAULT_REGION=eu-west-2 AWS_PAGER=""
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

say "Reading the $ENV_NAME deploy settings"
CONFIG="$(aws ssm get-parameter --name "/siteline/$ENV_NAME/deploy" --query Parameter.Value --output text)" \
  || die "could not read /siteline/$ENV_NAME/deploy: has terraform apply been run for $ENV_NAME?"
cfg() { jq -r "$1" <<<"$CONFIG"; }
CLUSTER="$(cfg .cluster)"
REPO_URL="$(cfg .repository_url)"
CONTAINER="$(cfg .container)"
LOG_GROUP="$(cfg .log_group)"
PLATFORM="linux/amd64"; [[ "$(cfg .cpu_arch)" == ARM64 ]] && PLATFORM="linux/arm64"
jq -c '{awsvpcConfiguration: {subnets: .subnets, securityGroups: .security_groups, assignPublicIp: "ENABLED"}}' \
  <<<"$CONFIG" >"$TMP/network.json"

image_exists() {
  aws ecr describe-images --repository-name "${REPO_URL#*/}" --image-ids "imageTag=$1" >/dev/null 2>&1
}

# --- 1. Image ------------------------------------------------------------------
if [[ -z "$IMAGE_TAG" ]]; then
  cd "$REPO_ROOT"
  DIRTY="$(git status --porcelain)"
  if [[ -n "$DIRTY" && "$ENV_NAME" == prod ]]; then
    die "you have uncommitted changes. Commit them first: prod only gets images built from a commit."
  fi
  IMAGE_TAG="$(git rev-parse --short=12 HEAD)"
  [[ -n "$DIRTY" ]] && IMAGE_TAG="$IMAGE_TAG-wip-$(date -u +%Y%m%d%H%M%S)"

  say "Logging in to ECR"
  aws ecr get-login-password | docker login --username AWS --password-stdin "${REPO_URL%%/*}" >/dev/null

  if image_exists "$IMAGE_TAG"; then
    say "Image $IMAGE_TAG is already in ECR, not rebuilding"
  else
    say "Building $IMAGE_TAG for $PLATFORM"
    docker build --platform "$PLATFORM" -t "$REPO_URL:$IMAGE_TAG" "$REPO_ROOT/siteline"
    say "Pushing"
    docker push "$REPO_URL:$IMAGE_TAG"
  fi
else
  image_exists "$IMAGE_TAG" || die "image $IMAGE_TAG is not in $REPO_URL"
fi
IMAGE="$REPO_URL:$IMAGE_TAG"

# --- 2. Task definitions ------------------------------------------------------------
# Copy the latest revision of a family, swap in the new image, register it; prints the new ARN.
register() {
  local family="$1"
  aws ecs describe-task-definition --task-definition "$family" --include TAGS --output json >"$TMP/current.json"
  jq --arg image "$IMAGE" --arg container "$CONTAINER" '
      .tags as $tags
      | .taskDefinition
      | .containerDefinitions |= map(if .name == $container then .image = $image else . end)
      | del(.taskDefinitionArn, .revision, .status, .requiresAttributes, .compatibilities,
            .registeredAt, .registeredBy, .deregisteredAt)
      | if ($tags | length) > 0 then .tags = $tags else . end' "$TMP/current.json" >"$TMP/new.json"
  aws ecs register-task-definition --cli-input-json "file://$TMP/new.json" \
    --query taskDefinition.taskDefinitionArn --output text
}

# --- 3. Migrations (before anything starts running the new code) ------------------------
say "Running database migrations"
MIGRATE_ARN="$(register "$(cfg .families.migrate)")"
TASK_ARN="$(aws ecs run-task --cluster "$CLUSTER" --task-definition "$MIGRATE_ARN" --launch-type FARGATE \
  --network-configuration "file://$TMP/network.json" --started-by "deploy-$IMAGE_TAG" \
  --query 'tasks[0].taskArn' --output text)"
[[ "$TASK_ARN" == arn:* ]] || die "the migrate task did not start"
aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$TASK_ARN" \
  || aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$TASK_ARN"   # a second 10-minute wait for slow migrations
EXIT_CODE="$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$TASK_ARN" \
  --query "tasks[0].containers[?name=='$CONTAINER'].exitCode | [0]" --output text)"
sleep 5 # the last lines can take a few seconds to reach CloudWatch
aws logs get-log-events --log-group-name "$LOG_GROUP" --log-stream-name "migrate/$CONTAINER/${TASK_ARN##*/}" \
  --start-from-head --output json 2>/dev/null | jq -r '.events[].message' >&2 || true
if [[ "$EXIT_CODE" != 0 ]]; then
  REASON="$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$TASK_ARN" --query 'tasks[0].stoppedReason' --output text)"
  die "migrations failed (exit code $EXIT_CODE: $REASON). Nothing else was changed; the site is still on the old version."
fi

# From here Terraform also uses the new image (it reads this parameter)
aws ssm put-parameter --name "/siteline/$ENV_NAME/image_tag" --value "$IMAGE_TAG" --type String --overwrite >/dev/null

# --- 4. Everything else ------------------------------------------------------------------
say "Registering the other task definitions"
for key in $(jq -r '.families | keys[] | select(. != "migrate")' <<<"$CONFIG"); do
  ARN="$(register "$(cfg ".families[\"$key\"]")")"
  printf -v "ARN_${key//-/_}" '%s' "$ARN"
done

say "Updating the web and worker services"
aws ecs update-service --cluster "$CLUSTER" --service "$(cfg .services.web)" --task-definition "$ARN_web" >/dev/null
aws ecs update-service --cluster "$CLUSTER" --service "$(cfg .services.worker)" --task-definition "$ARN_worker" >/dev/null

say "Waiting for the new version to be healthy (usually 2-5 minutes)"
aws ecs wait services-stable --cluster "$CLUSTER" --services "$(cfg .services.web)" "$(cfg .services.worker)" \
  || aws ecs wait services-stable --cluster "$CLUSTER" --services "$(cfg .services.web)" "$(cfg .services.worker)"

# If the new tasks kept failing, ECS rolls back to the previous version and still reports "stable"
RUNNING="$(aws ecs describe-services --cluster "$CLUSTER" --services "$(cfg .services.web)" \
  --query "services[0].deployments[?status=='PRIMARY'] | [0].[taskDefinition, rolloutState]" --output text)"
[[ "$RUNNING" == "$ARN_web"$'\t'COMPLETED ]] || die "web did not switch to the new version ($RUNNING). ECS kept the previous version running. See the logs: aws logs tail $LOG_GROUP --since 30m"

say "Deployed $IMAGE_TAG to $ENV_NAME"
