# Linux systemd deployment

The daemon polls the public quota endpoint directly. It keeps the previous
snapshot in memory and in `/var/lib/quota-monitor/state.json`; malformed or
empty responses never replace a valid baseline.

## Install

Run these commands on the Linux server from a checked-out repository:

```bash
sudo useradd --system --home /opt/quota-monitor --shell /usr/sbin/nologin quota-monitor || true
sudo install -d -o quota-monitor -g quota-monitor /opt/quota-monitor
sudo cp -a . /opt/quota-monitor/
sudo chown -R quota-monitor:quota-monitor /opt/quota-monitor
sudo -u quota-monitor python3 -m venv /opt/quota-monitor/.venv
sudo -u quota-monitor /opt/quota-monitor/.venv/bin/pip install /opt/quota-monitor

sudo install -d -m 0750 -o root -g quota-monitor /etc/quota-monitor
sudo install -m 0640 -o root -g quota-monitor \
  deploy/systemd/config.production.example.json /etc/quota-monitor/config.json
sudo install -m 0640 -o root -g quota-monitor \
  deploy/systemd/quota-monitor.env.example /etc/quota-monitor/quota-monitor.env
sudo install -m 0644 deploy/systemd/quota-monitor.service \
  /etc/systemd/system/quota-monitor.service

sudo systemctl daemon-reload
sudo systemctl enable --now quota-monitor
```

Keep secrets only in `/etc/quota-monitor/quota-monitor.env`. Never commit that
file. Check operation with:

```bash
systemctl status quota-monitor --no-pager
journalctl -u quota-monitor -f
```

## Safe migration from GitHub Actions

1. Leave `QUOTA_MONITOR_SHADOW=1` for at least 24 hours. Shadow mode writes
   snapshots and dashboard JSON but suppresses notifications and ReleaseSignal.
2. Compare `/var/lib/quota-monitor/web/quota.json` with the GitHub Actions data.
3. Set `QUOTA_MONITOR_SHADOW=0`, restart the service, and verify one normal
   polling cycle.
4. Disable the external scheduler and scheduled Actions trigger. Keep manual
   workflow dispatch as a fallback.

```bash
sudo systemctl restart quota-monitor
```

Twenty seconds is the enforced minimum. Successful cycles use a small random
jitter; request failures use exponential backoff up to 15 minutes. HTTP 403,
429 and transient 5xx responses honor `Retry-After` when supplied.
