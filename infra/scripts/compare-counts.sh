#!/usr/bin/env bash
# Compare two `manage.py model_counts` outputs (PythonAnywhere vs AWS). Linux/macOS version of compare-counts.ps1.
#   infra/scripts/compare-counts.sh counts-pythonanywhere.txt counts-aws.txt
set -euo pipefail
[[ $# -eq 2 ]] || { echo "Usage: $0 <before.txt> <after.txt>" >&2; exit 2; }
# Keep only "app.Model 123" and "TOTAL 123" lines (drops log lines such as "AXES: BEGIN ...")
only_counts() { tr -d '\r' <"$1" | grep -E '^([A-Za-z_]+\.[A-Za-z0-9_]+|TOTAL) [0-9]+$' | sort; }
if diff <(only_counts "$1") <(only_counts "$2"); then
  echo "MATCH: every table has the same number of rows ($(only_counts "$1" | grep -c . ) lines compared)."
else
  echo "DIFFERENT: the lines above differ (< is $1, > is $2). Do not switch over; see migrate-data.md." >&2
  exit 1
fi
