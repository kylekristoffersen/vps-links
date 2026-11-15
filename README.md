# VPS links and login

Kyle's front door to the tools on the VPS (Ubuntu 20.04, administered as root):

- **https://107-173-7-177.sslip.io/links/**: the link page, admin login only.
- **The in-page login (toolgate)** used by the tools site: the converter,
  the Crossbuilder, the link page and the single-page tools. It replaces the
  browser's password popup.
- **The RSS filter is not behind toolgate.** It has its own in-page sign-in
  (repo `kylekristoffersen/rss-filter`, same `kyle` password), and it checks
  the torrent client's secret feed address itself, so nginx must pass that
  address through unchanged. Its nginx site is managed by that repo's
  `deploy/apply-nginx.sh`; do not copy a toolgate config over it.

## The link page

`site/index.html`. Single-page tools live beside it in `site/tools/` and
open at `/links/tools/<file>.html`. To add or change a tool, edit the page
(or drop the file into `site/tools/`) and push to `main`. The VPS pulls within
a minute, and nginx serves `site/` straight from the clone at `/opt/vps-links`.

**Hiding tools:** Edit (top right) shows a Hide button on every card;
hidden tools move into a collapsed "Hidden" section, where Show brings them
back. The list is saved by toolgate per login (`/_gate/prefs`, stored in
`/var/lib/toolgate/prefs.json`), so it is the same on every device. Each
card's `data-id` is what is remembered, so keep it when editing a card.
Page tests: `node test/test_links_page.js` (needs `npm install jsdom`).

The single-page tools keep their data in the browser (localStorage), so each
device starts empty. Unseen's lists come across by importing the
`unseen-lists.json` export once per device.

## The login (toolgate)

`gate/toolgate.py`, standard library only, runs as user `toolgate` on
127.0.0.1:8600 with the system Python 3.8 (`deploy/toolgate.service`).
Tests: `python3 gate/test_toolgate.py`.

nginx asks it before serving any page (`auth_request`), using
`deploy/nginx/toolgate.conf`:

| Check | Who passes |
|---|---|
| `/_gate/check/team` | the team login (`veit`) and the admin login |
| `/_gate/check/admin` | the admin login (`kyle`) only |

The tools server block requires `team` (so the converter is open to the
teammate); `/crossbuilder/` and `/links/` require `admin`.

- **Logins** are read from the existing htpasswd files:
  `/etc/nginx/.admin-tools-htpasswd` (admin) and
  `/etc/nginx/.veit-tools-htpasswd` (team). Change a password with
  `htpasswd /etc/nginx/.admin-tools-htpasswd kyle`. A change takes effect at
  once and signs that login out on every device.
- **Sessions:** a signed cookie for `107-173-7-177.sslip.io`. "Keep me signed in"
  lasts 30 days; otherwise 12 hours, ending when the browser closes.
  `/_gate/logout` signs out this device. To sign out every device at once,
  replace the secret and restart:
  `python3 /opt/vps-links/gate/toolgate.py new-secret > /etc/toolgate/secret && systemctl restart toolgate`.
- **Wrong passwords:** after 5 wrong tries from one address (or 20 for one
  login name) within 15 minutes, sign-in is refused until the window passes.
  Failed and successful sign-ins are logged: `journalctl -u toolgate`.
- **Signed in but not allowed** (the teammate on `/links/`): a "Not
  available" page with a link to the converter.

## On the VPS

| Piece | Where |
|---|---|
| Clone | `/opt/vps-links` (deploy key `/root/.ssh/links_deploy`, SSH alias `github-links`) |
| Auto-update | `/etc/cron.d/vps-links` runs `deploy/update.sh` every minute; it restarts toolgate when `gate/` changed. Log: `/var/log/vps-links-update.log` |
| Login service | `/etc/systemd/system/toolgate.service` (copy of `deploy/toolgate.service`), user `toolgate` (in group `www-data` to read the admin password file); secret `/etc/toolgate/secret`; saved settings `/var/lib/toolgate/prefs.json` |
| nginx | `/etc/nginx/snippets/toolgate.conf`, `/etc/nginx/snippets/vps-links.conf`, and the site file `/etc/nginx/sites-available/veit-tools`, all copies of `deploy/nginx/` |

The nginx and service files are copies, so a pull never changes them. After
changing one here, copy it over on the VPS, then `nginx -t && systemctl reload nginx`
(or `systemctl daemon-reload && systemctl restart toolgate`).
