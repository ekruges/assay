#!/bin/sh
# Publish the built site to the web host: upload beside the live tree, swap whole, keep today's pages
# under wayback/<day>, prune the days outside retention, and list the days kept.
#
#   ASSAY_SITE_DIR      the built site (default /srv/site)
#   ASSAY_PUBLISH_SSH   user@host of the web server
#   ASSAY_PUBLISH_DIR   the served folder on that host
set -u
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_dir"
export ASSAY_SITE_DIR=${ASSAY_SITE_DIR:-/srv/site}
: "${ASSAY_PUBLISH_SSH:?user@host of the web server}"
target=${ASSAY_PUBLISH_DIR:?ASSAY_PUBLISH_DIR names the served folder}
as_of=$(python3 -c 'import json; print(json.load(open("data/index.json"))["as_of"])')
# Upload beside the live tree and swap whole. The wayback folder carries over, then today's tree is
# hard-linked into it, without the price files and the history records: a copy of every page as
# published, at no cost while the inodes are shared.
tar -C "$ASSAY_SITE_DIR" -cf - . | ssh "$ASSAY_PUBLISH_SSH" "
  rm -rf '$target.new' '$target.old' && mkdir -p '$target.new' && tar -xf - -C '$target.new' &&
  { [ -d '$target' ] && mv '$target' '$target.old'; mv '$target.new' '$target'; } &&
  { [ -d '$target.old/wayback' ] && mv '$target.old/wayback' '$target/wayback'; rm -rf '$target/wayback/$as_of'; mkdir -p '$target/wayback/$as_of'; } &&
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
exit "$status"
