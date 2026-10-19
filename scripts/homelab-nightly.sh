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
  as_of=$(python3 -c 'import json; print(json.load(open("data/index.json"))["as_of"])')
  # Upload beside the live tree and swap whole. The wayback folder carries over, then today's tree is
  # hard-linked into it, without the price files and the history records: a copy of every page as
  # published, at no cost while the inodes are shared.
  tar -C "$ASSAY_SITE_DIR" -cf - . | ssh "$ASSAY_PUBLISH_SSH" "
    rm -rf '$target.new' '$target.old' && mkdir -p '$target.new' && tar -xf - -C '$target.new' &&
    { [ -d '$target' ] && mv '$target' '$target.old'; mv '$target.new' '$target'; } &&
    { [ -d '$target.old/wayback' ] && mv '$target.old/wayback' '$target/wayback'; mkdir -p '$target/wayback/$as_of'; } &&
    cd '$target' && for f in *; do case \"\$f\" in wayback|prices|history) ;; *) cp -al \"\$f\" 'wayback/$as_of/' ;; esac; done && rm -rf '$target.old'"
  status=$?
  if [ "$status" -eq 0 ]; then
    days=$(ssh "$ASSAY_PUBLISH_SSH" "ls '$target/wayback' | grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'")
    drop=$(printf '%s\n' $days | python3 -c '
import sys
from assay.export import retained
days = [d.strip() for d in sys.stdin if d.strip()]
keep = set(retained(days, max(days))) if days else set()
print(" ".join(d for d in days if d not in keep))')
    for d in $drop; do ssh "$ASSAY_PUBLISH_SSH" "rm -rf '$target/wayback/$d'"; done
    ssh "$ASSAY_PUBLISH_SSH" "cd '$target/wayback' && ls | grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' | sort | sed 's/.*/\"&\"/' | paste -sd, - | sed 's/.*/[&]/' > dates.json"
  fi
fi
exit "$status"
