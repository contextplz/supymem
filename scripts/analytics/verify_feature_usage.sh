#!/usr/bin/env bash
# Per-feature views/uses/errors for the last N hours (zero-use features included) plus
# CloudWatch error_event counts. Read-only: a one-off ECS task with PGOPTIONS
# default_transaction_read_only=on runs scripts/analytics/verify_feature_usage.py inside the
# core-api container, prints aggregate VERIFY_ROW lines, and this script renders them.
#
#   scripts/analytics/verify_feature_usage.sh [HOURS=24]
#   scripts/analytics/verify_feature_usage.sh 6 --json      # parsed rows instead of tables
#
# Needs: aws CLI (credentials for the prod account, ecs:RunTask + logs:*), python3.
# Prints an error digest (component > source > intent > code, internal vs external accounts, versions).
# Overrides (defaults are the verified prod values): AWS_REGION, VERIFY_CLUSTER, VERIFY_TASK_FAMILY,
# VERIFY_SUBNETS, VERIFY_SG, VERIFY_CONTAINER, VERIFY_LOG_GROUP, VERIFY_WORKER_LOG_GROUP, VERIFY_SKIP_CLOUDWATCH=1.
set -euo pipefail

HOURS="${1:-24}"
FORMAT="${2:-}"
[[ "$HOURS" =~ ^[0-9]+$ && "$HOURS" -ge 1 && "$HOURS" -le 720 ]] || { echo "usage: $0 [HOURS 1-720] [--json]" >&2; exit 2; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REGION="${AWS_REGION:-us-east-1}"
CLUSTER="${VERIFY_CLUSTER:?Set VERIFY_CLUSTER to your production cluster and retry.}"
FAMILY="${VERIFY_TASK_FAMILY:?Set VERIFY_TASK_FAMILY to your Core task family and retry.}"
SUBNETS="${VERIFY_SUBNETS:-subnet-02f800211cfee1dcc,subnet-03b0398cc0fe76c78}"
SG="${VERIFY_SG:-sg-04ff610fe37431f2f}"
CONTAINER="${VERIFY_CONTAINER:-app}"
CORE_GROUP="${VERIFY_LOG_GROUP:?Set VERIFY_LOG_GROUP to your Core log group and retry.}"
WORKER_GROUP="${VERIFY_WORKER_LOG_GROUP:?Set VERIFY_WORKER_LOG_GROUP to your worker log group and retry.}"
# Every prod service writes error_event lines (see scripts/infra/configure_error_alarm.py).
OTHER_GROUPS="${VERIFY_OTHER_LOG_GROUPS:-}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# The payload travels gzip+base64 inside the run-task override (ECS caps overrides at 8192 chars).
OVERRIDES="$(HOURS="$HOURS" CONTAINER="$CONTAINER" SRC="$HERE/verify_feature_usage.py" python3 - <<'PY'
import base64, gzip, json, os
src = open(os.environ["SRC"]).read()
# The report renderer runs locally; the container only needs the queries and the payload entry point.
src = src[:src.index("\ndef _table(")] + src[src.index('\nif os.environ.get("VERIFY_PAYLOAD")'):]
code = base64.b64encode(gzip.compress(src.encode(), 9)).decode()
boot = f"import base64,gzip;exec(gzip.decompress(base64.b64decode('{code}')).decode())"
overrides = {"containerOverrides": [{"name": os.environ["CONTAINER"],
    "environment": [{"name": "PGOPTIONS", "value": "-c default_transaction_read_only=on"},
                    {"name": "APP_ROLE", "value": "report"}, {"name": "VERIFY_PAYLOAD", "value": "1"},
                    {"name": "VERIFY_HOURS", "value": os.environ["HOURS"]}],
    "command": ["python", "-c", boot]}]}
text = json.dumps(overrides)
assert len(text) < 8000, f"override too large ({len(text)})"
print(text)
PY
)"

echo "Resolving task definition ${FAMILY} ..." >&2
TD="$(aws ecs describe-task-definition --task-definition "$FAMILY" --region "$REGION" \
      --query 'taskDefinition.taskDefinitionArn' --output text)"
PREFIX="$(aws ecs describe-task-definition --task-definition "$TD" --region "$REGION" \
      --query 'taskDefinition.containerDefinitions[0].logConfiguration.options."awslogs-stream-prefix"' --output text)"

echo "Starting read-only task (${HOURS}h window) ..." >&2
TASK="$(aws ecs run-task --region "$REGION" --cluster "$CLUSTER" --launch-type FARGATE --task-definition "$TD" \
  --network-configuration "awsvpcConfiguration={subnets=[${SUBNETS}],securityGroups=[${SG}],assignPublicIp=DISABLED}" \
  --overrides "$OVERRIDES" --query 'tasks[0].taskArn' --output text)"
[[ "$TASK" == arn:* ]] || { echo "run-task failed to start" >&2; exit 1; }
TASK_ID="${TASK##*/}"
aws ecs wait tasks-stopped --region "$REGION" --cluster "$CLUSTER" --tasks "$TASK"
EXIT_CODE="$(aws ecs describe-tasks --region "$REGION" --cluster "$CLUSTER" --tasks "$TASK" \
      --query 'tasks[0].containers[0].exitCode' --output text)"

# Log delivery lags the task stopping; wait for the final marker row.
: >"$WORK/rows.txt"
for _ in 1 2 3 4 5 6 7 8; do
  aws logs filter-log-events --region "$REGION" --log-group-name "$CORE_GROUP" \
    --log-stream-names "${PREFIX}/${CONTAINER}/${TASK_ID}" --filter-pattern VERIFY_ROW \
    --query 'events[].message' --output text | tr '\t' '\n' >"$WORK/rows.txt" || true
  grep -q '"t": "done"' "$WORK/rows.txt" && break
  sleep 5
done
if ! grep -q '"t": "done"' "$WORK/rows.txt"; then
  echo "payload did not finish (container exit code ${EXIT_CODE}); stream ${PREFIX}/${CONTAINER}/${TASK_ID}" >&2
  aws logs get-log-events --region "$REGION" --log-group-name "$CORE_GROUP" \
    --log-stream-name "${PREFIX}/${CONTAINER}/${TASK_ID}" --limit 15 --query 'events[].message' --output text >&2 || true
  exit 1
fi

CW_ARGS=()
if [[ "${VERIFY_SKIP_CLOUDWATCH:-}" != "1" ]]; then
  NOW="$(date +%s)"
  QUERY='fields @message | filter @message like /error_event/'
  QUERY+=' | parse @message /"source": "(?<source>[^"]*)"/ | parse @message /"feature": "(?<feature>[^"]*)"/'
  QUERY+=' | parse @message /"code": "(?<code>[^"]*)"/ | parse @message /"component": "(?<component>[^"]*)"/ | parse @message /"intent": "(?<intent>[^"]*)"/'
  QUERY+=' | stats count(*) as n by component, source, feature, intent, code | sort n desc | limit 100'
  QID="$(aws logs start-query --region "$REGION" --log-group-names "$CORE_GROUP" "$WORKER_GROUP" $OTHER_GROUPS \
        --start-time $((NOW - HOURS * 3600)) --end-time "$NOW" --query-string "$QUERY" --query queryId --output text || true)"
  if [[ -n "${QID:-}" && "$QID" != "None" ]]; then
    for _ in $(seq 1 30); do
      aws logs get-query-results --region "$REGION" --query-id "$QID" --output json >"$WORK/cw.json" || true
      grep -q '"status": "Complete"' "$WORK/cw.json" && break
      sleep 2
    done
    grep -q '"status": "Complete"' "$WORK/cw.json" && CW_ARGS=(--cw "$WORK/cw.json")
  fi
  [[ ${#CW_ARGS[@]} -gt 0 ]] || echo "CloudWatch query unavailable; skipping the error_event cross-check" >&2
fi

JSON_ARGS=()
[[ "$FORMAT" == "--json" ]] && JSON_ARGS=(--json)
python3 "$HERE/verify_feature_usage.py" --rows "$WORK/rows.txt" --hours "$HOURS" ${CW_ARGS[@]+"${CW_ARGS[@]}"} ${JSON_ARGS[@]+"${JSON_ARGS[@]}"}
