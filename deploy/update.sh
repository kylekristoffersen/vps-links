#!/bin/sh
# Runs every minute from /etc/cron.d/vps-links (as root): pull the repo.
# nginx serves site/ straight from the clone, so pages need nothing more.
# If the login service or the Trello sync page changed, restart it.
exec 9>/var/lock/vps-links-update.lock
flock -n 9 || exit 0
cd /opt/vps-links || exit 1
OLD=$(git rev-parse HEAD)
git pull -q --ff-only || exit 1
NEW=$(git rev-parse HEAD)
[ "$OLD" = "$NEW" ] && exit 0
echo "$(date '+%F %T') updated $OLD -> $NEW"
if ! git diff --quiet "$OLD" "$NEW" -- gate/; then
    systemctl restart toolgate
fi
if ! git diff --quiet "$OLD" "$NEW" -- trello-sync/; then
    systemctl restart trello-sync
fi
