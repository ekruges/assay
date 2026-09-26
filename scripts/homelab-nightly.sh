#!/bin/sh
# The unattended nightly on the homelab container: runs scripts/nightly.sh with its output kept as
# the public log, then publishes the site to the NAS web root with an atomic directory swap.
#
#   ASSAY_SITE_DIR      site output folder (default /srv/site)
#   ASSAY_PUBLISH_SSH   user@host of the web server; publishing is skipped when unset
#   ASSAY_PUBLISH_DIR   the served folder on that host, replaced whole after a successful run
set -u

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_dir"
export ASSAY_SITE_DIR=${ASSAY_SITE_DIR:-/srv/site}
export ASSAY_LOGS=${ASSAY_LOGS:-$project_dir/.state/logs}
mkdir -p "$ASSAY_LOGS" "$ASSAY_SITE_DIR/logs"
day=$(date +%F)
log=$ASSAY_LOGS/$day.log

started=$(date -u +%FT%TZ)
sh scripts/nightly.sh > "$log" 2>&1
status=$?
cp "$log" "$ASSAY_SITE_DIR/logs/$day.log"
find "$ASSAY_LOGS" -name '*.log' -mtime +92 -delete
runs=${ASSAY_RUNS:-$project_dir/.state/runs.jsonl}

if [ "$status" -ne 0 ]; then
  # A run that did not complete still goes on the public log: record it, rebuild the run log page
  # from the last verified tree, and publish only that page, the logs and status.json.
  python3 -m assay.runlog --runs "$runs" --failed "$day" --started "$started" --log "logs/$day.log" >> "$log" 2>&1
  python3 -m assay.site --data data --out "$ASSAY_SITE_DIR" --only runs.html --runs "$runs" --logs "$ASSAY_LOGS" >> "$log" 2>&1
  cp "$log" "$ASSAY_SITE_DIR/logs/$day.log"
  if [ -n "${ASSAY_PUBLISH_SSH:-}" ] && [ -d "$ASSAY_SITE_DIR/logs" ]; then
    target=${ASSAY_PUBLISH_DIR:?ASSAY_PUBLISH_DIR names the served folder}
    tar -C "$ASSAY_SITE_DIR" -cf - runs.html status.json logs | ssh "$ASSAY_PUBLISH_SSH" "mkdir -p '$target' && tar -xf - -C '$target'"
  fi
  exit "$status"
fi

if [ -n "${ASSAY_PUBLISH_SSH:-}" ]; then
  target=${ASSAY_PUBLISH_DIR:?ASSAY_PUBLISH_DIR names the served folder}
  tar -C "$ASSAY_SITE_DIR" -cf - . | ssh "$ASSAY_PUBLISH_SSH" "
    rm -rf '$target.new' '$target.old' && mkdir -p '$target.new' && tar -xf - -C '$target.new' &&
    { [ -d '$target' ] && mv '$target' '$target.old'; mv '$target.new' '$target' && rm -rf '$target.old'; }"
  status=$?
fi
exit "$status"
