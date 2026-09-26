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
export ASSAY_GRADE_HISTORY=${ASSAY_GRADE_HISTORY:-$project_dir/.state/grade_history.json}
run_day=$(date +%F)
started=$(date -u +%FT%TZ)
history_index=${ASSAY_HISTORY_INDEX:-$project_dir/.state/history}
detail_dir=${ASSAY_DETAIL:-$project_dir/.state/detail}
runs=${ASSAY_RUNS:-$project_dir/.state/runs.jsonl}
logs=${ASSAY_LOGS:-$project_dir/.state/logs}

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

# The rescan's records: the slim index and the inputs behind every letter, kept daily for
# 92 days and monthly before that; then the run record for the public log.
python3 -m assay.export --data data --history-index "$history_index" --detail "$detail_dir" --snapshot > snapshot-summary.json
python3 -m assay.runlog --runs "$runs" --data data --verification verification-summary.json \
  --history "$ASSAY_GRADE_HISTORY" --started "$started" --log "logs/$run_day.log" > run-record.json

# Static site: prices for the charts, each company's own description, the filings calendar,
# then every page, the exports and one PDF report per graded company. Skipped unless
# ASSAY_SITE_DIR names the output folder.
if [ -n "${ASSAY_SITE_DIR:-}" ]; then
  as_of=$(python3 -c 'import json; print(json.load(open("data/index.json"))["as_of"])')
  descriptions=${ASSAY_DESCRIPTIONS:-$project_dir/.state/descriptions.json}
  mkdir -p "$(dirname "$descriptions")"
  python3 -m assay.prices --data data --market-db .cache/market/market.sqlite3 --as-of "$as_of" --out .state/prices.json
  python3 -m assay.prices --data data --market-db .cache/market/market.sqlite3 --as-of "$as_of" --full --out-dir "$ASSAY_SITE_DIR/prices"
  python3 -m assay.describe --cache-dir .cache/sec --data data --all --out "$descriptions"
  python3 -m assay.calendar --cache-dir .cache/sec --data data --as-of "$as_of" --out .state/filing_calendar.json
  python3 -m assay.site --data data --out "$ASSAY_SITE_DIR" --all \
    --history "$ASSAY_GRADE_HISTORY" --history-index "$history_index" --detail "$detail_dir" \
    --runs "$runs" --logs "$logs" \
    --prices .state/prices.json --descriptions "$descriptions" --calendar .state/filing_calendar.json \
    --asset-base img/ --pdf --site-root "${ASSAY_SITE_ROOT:-/}"
fi
