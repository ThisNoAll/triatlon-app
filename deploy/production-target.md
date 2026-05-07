# Production Target (Default)

This project uses a **Tailscale-based production environment** by default.

- Public URL: `https://homeapp.tail4b6c6a.ts.net/`
- Server type: self-hosted Linux machine (systemd services)
- App directory: `/opt/triatlon`
- Main app service: `triatlon`
- Reverse proxy: `nginx`
- Self-heal timer/service: `triatlon-selfheal.timer` / `triatlon-selfheal.service`

## Standard production update flow

Run these on the production server:

```bash
cd /opt/triatlon
git pull
./.venv/bin/pip install -r requirements.txt
sudo systemctl restart triatlon
sudo systemctl restart nginx
sudo systemctl start triatlon-selfheal.service
```

## Health checks

```bash
systemctl status triatlon --no-pager
systemctl status nginx --no-pager
systemctl status tailscaled --no-pager
systemctl status triatlon-selfheal.timer --no-pager
journalctl -u triatlon -n 50 --no-pager
journalctl -u triatlon-selfheal.service -n 50 --no-pager
curl -I https://homeapp.tail4b6c6a.ts.net/
```
