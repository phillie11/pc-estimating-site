#!/usr/bin/env bash
# Run a manage.py command on AWS. Linux/macOS/CI version of run-command.ps1 (same behaviour).
#
#   infra/scripts/run-command.sh staging model_counts              # one-off task; prints its output
#   infra/scripts/run-command.sh prod check_file_scans --retrigger
#   infra/scripts/run-command.sh prod --exec createsuperuser       # interactive, inside a running web task
#
# One-off tasks start a fresh container (about a minute), run the command, print its output and exit
# with its exit code. --exec opens an interactive session in a running web container instead (needs the
# AWS Session Manager plugin); use it for anything that asks questions, like createsuperuser.
# Do not put passwords on the command line: they are recorded in the task's settings.
set -euo pipefail

usage() { echo "Usage: $0 staging|prod [--exec] <manage.py command> [args...]" >&2; exit 2; }
die() { echo "ERROR: $*" >&2; exit 1; }
say() { echo "==> $*" >&2; }

ENV_NAME="${1:-}"
[[ "$ENV_NAME" == staging || "$ENV_NAME" == prod ]] || usage
shift
EXEC=0
if [[ "${1:-}" == --exec ]]; then EXEC=1; shift; fi
[[ $# -gt 0 ]] || usage

for tool in aws jq; do command -v "$tool" >/dev/null || die "$tool is not installed"; done
export AWS_REGION=eu-west-2 AWS_DEFAULT_REGION=eu-west-2 AWS_PAGER=""
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

CONFIG="$(aws ssm get-parameter --name "/siteline/$ENV_NAME/deploy" --query Parameter.Value --output text)" \
  || die "could not read /siteline/$ENV_NAME/deploy: has terraform apply been run for $ENV_NAME?"
cfg() { jq -r "$1" <<<"$CONFIG"; }
CLUSTER="$(cfg .cluster)"
CONTAINER="$(cfg .container)"
LOG_GROUP="$(cfg .log_group)"

if [[ $EXEC == 1 ]]; then
  command -v session-manager-plugin >/dev/null || die "install the AWS Session Manager plugin first (infra/README.md)"
  TASK_ARN="$(aws ecs list-tasks --cluster "$CLUSTER" --service-name "$(cfg .services.web)" --desired-status RUNNING \
    --query 'taskArns[0]' --output text)"
  [[ "$TASK_ARN" == arn:* ]] || die "no web task is running in $ENV_NAME"
  say "Opening python manage.py $* in ${TASK_ARN##*/}"
  exec aws ecs execute-command --cluster "$CLUSTER" --task "$TASK_ARN" --container "$CONTAINER" --interactive \
    --command "python manage.py $(printf '%q ' "$@")"
fi

jq -c '{awsvpcConfiguration: {subnets: .subnets, securityGroups: .security_groups, assignPublicIp: "ENABLED"}}' \
  <<<"$CONFIG" >"$TMP/network.json"
# RUN_COMMAND_RAW=1 (used by load-data.sh): the arguments are the whole command, not manage.py arguments
PREFIX='["python", "manage.py"]'
[[ "${RUN_COMMAND_RAW:-0}" == 1 ]] && PREFIX='[]'
# Arguments go to jq NUL-separated on stdin, so ones starting with "-" are never read as jq options
printf '%s\0' "$@" | jq -cRs --arg c "$CONTAINER" --argjson prefix "$PREFIX" \
  '{containerOverrides: [{name: $c, command: ($prefix + (split("\u0000") | .[:-1]))}]}' >"$TMP/overrides.json"

if [[ "${RUN_COMMAND_RAW:-0}" == 1 ]]; then say "Starting a one-off task: $1 ..."; else say "Starting a one-off task: python manage.py $*"; fi
TASK_ARN="$(aws ecs run-task --cluster "$CLUSTER" --task-definition "$(cfg .families.manage)" --launch-type FARGATE \
  --network-configuration "file://$TMP/network.json" --overrides "file://$TMP/overrides.json" \
  --started-by run-command --query 'tasks[0].taskArn' --output text)"
[[ "$TASK_ARN" == arn:* ]] || die "the task did not start"
say "Waiting for it to finish (task ${TASK_ARN##*/})"
aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$TASK_ARN" \
  || aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$TASK_ARN"

# The command's output, exactly as printed (stdout), so it can be saved with > file.txt
sleep 5 # the last lines can take a few seconds to reach CloudWatch
aws logs get-log-events --log-group-name "$LOG_GROUP" --log-stream-name "manage/$CONTAINER/${TASK_ARN##*/}" \
  --start-from-head --output json | jq -r '.events[].message'

EXIT_CODE="$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$TASK_ARN" \
  --query "tasks[0].containers[?name=='$CONTAINER'].exitCode | [0]" --output text)"
if [[ "$EXIT_CODE" != 0 ]]; then
  REASON="$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$TASK_ARN" --query 'tasks[0].stoppedReason' --output text)"
  die "the command failed (exit code $EXIT_CODE: $REASON)"
fi
