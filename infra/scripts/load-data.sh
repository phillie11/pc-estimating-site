#!/usr/bin/env bash
# Load a PythonAnywhere database export into the AWS database. Linux/macOS version of load-data.ps1.
# See infra/scripts/migrate-data.md for the whole procedure.
#
#   infra/scripts/load-data.sh staging siteline-data.json.gz
#
# Uploads the file to the environment's private transfer bucket (auto-deleted after 7 days), then runs
# a one-off task that downloads it, runs loaddata in one transaction, resets the ID sequences and
# prints the row counts. The target database must be freshly migrated and empty.
set -euo pipefail

die() { echo "ERROR: $*" >&2; exit 1; }
ENV_NAME="${1:-}"
FILE="${2:-}"
[[ ( "$ENV_NAME" == staging || "$ENV_NAME" == prod ) && -f "$FILE" ]] || die "Usage: $0 staging|prod <export.json.gz>"
[[ "$FILE" == *.json.gz ]] || die "expected a .json.gz file made by export_data (see migrate-data.md)"

export AWS_REGION=eu-west-2 AWS_DEFAULT_REGION=eu-west-2 AWS_PAGER=""
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="$(aws ssm get-parameter --name "/siteline/$ENV_NAME/deploy" --query Parameter.Value --output text)"
BUCKET="$(jq -r .transfer_bucket <<<"$CONFIG")"
KEY="import/$(date -u +%Y%m%d-%H%M%S)-$(basename "$FILE")"

read -r -p "Load $FILE into the $ENV_NAME database? Type $ENV_NAME to continue: " ANSWER
[[ "$ANSWER" == "$ENV_NAME" ]] || die "cancelled"

echo "==> Uploading to s3://$BUCKET/$KEY" >&2
aws s3 cp "$FILE" "s3://$BUCKET/$KEY" --only-show-errors

RUN_COMMAND_RAW=1 "$HERE/run-command.sh" "$ENV_NAME" python -c "$(cat "$HERE/lib/load_fixture.py")" "$BUCKET" "$KEY"
