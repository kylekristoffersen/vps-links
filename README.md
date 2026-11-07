# VPS links

Kyle's link page for the tools hosted on the VPS:
**https://107-173-7-177.sslip.io/links/** (admin login).

- The page: `site/index.html`. To add or change a tool, edit the `<li>` list
  there and push to `main`; the VPS pulls within a minute
  (`/etc/cron.d/vps-links`), and nginx serves `site/` straight from the clone
  at `/opt/vps-links`.
- nginx: `deploy/nginx-links.conf`, copied to
  `/etc/nginx/snippets/vps-links.conf` and included in the HTTPS server block
  of `/etc/nginx/sites-available/veit-tools`. It is a copy, so after changing
  it in the repo, copy it again on the VPS, then `nginx -t && systemctl reload nginx`.
- GitHub access from the VPS: read-only deploy key
  `/root/.ssh/links_deploy`, SSH alias `github-links`.
