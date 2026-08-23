#!/bin/sh
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_dir"

if [ -f "$project_dir/.env.local" ]; then
  set -a
  . "$project_dir/.env.local"
  set +a
fi

: "${ASSAY_SEC_USER_AGENT:?set ASSAY_SEC_USER_AGENT with a real contact email}"
: "${APCA_API_KEY_ID:?set APCA_API_KEY_ID}"
: "${APCA_API_SECRET_KEY:?set APCA_API_SECRET_KEY}"
forecast_log=${ASSAY_FORECAST_LOG:-forecast_log.jsonl}
run_day=$(date +%F)

python3 -m assay \
  --all \
  --refresh \
  --sync-market \
  --output-dir data \
  --json > run-summary.json

python3 -m assay --verify-output data --json > verification-summary.json

python3 -m assay.forecast --log "$forecast_log" settle --as-of "$run_day" \
  > forecast-settlement-summary.json

python3 -m assay.forecast --log "$forecast_log" publish \
  --as-of "$run_day" --output data/forecasts.json > /dev/null
