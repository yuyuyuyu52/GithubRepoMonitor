# Deploying monitor on Debian

Target: Debian 12 (bookworm) or newer, systemd-based.

## Prereqs

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip sqlite3 logrotate rsync
```

## First install

```bash
git clone <this-repo> ~/monitor-src
cd ~/monitor-src
sudo ./deploy/install.sh
```

Then edit the config:

```bash
sudo -e /etc/monitor/monitor.env     # paste real GITHUB_TOKEN / MINIMAX_API_KEY /
                                     # TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
sudo -e /etc/monitor/config.json     # tune keywords / thresholds
```

Start it:

```bash
sudo systemctl enable --now monitor.service
sudo systemctl enable --now monitor-health.timer monitor-backup.timer
```

Watch it:

```bash
sudo journalctl -u monitor.service -f
sudo systemctl list-timers monitor-*
```

## Upgrade

From the repo checkout:

```bash
git pull
sudo ./deploy/install.sh     # rsync + pip install -e + daemon-reload
sudo systemctl restart monitor.service
```

`install.sh` preserves `/etc/monitor/*` and the existing `/var/lib/monitor/monitor.db`. Only the code tree at `/opt/monitor` and the systemd units are refreshed.

## Inspect

| Question                           | Command                                                     |
|------------------------------------|-------------------------------------------------------------|
| Daemon running?                    | `systemctl status monitor.service`                          |
| Recent logs?                       | `journalctl -u monitor.service --since '1 hour ago'`        |
| App log file?                      | `sudo tail -f /var/log/monitor/app.log`                     |
| Healthcheck firing?                | `systemctl list-timers monitor-health.timer`                |
| Last healthcheck result?           | `journalctl -u monitor-health.service -n 50`                |
| Last backup?                       | `ls -la /var/lib/monitor/backups/`                          |
| DB size?                           | `sudo du -sh /var/lib/monitor/monitor.db`                   |

## Restore a backup

```bash
sudo systemctl stop monitor.service
sudo -u monitor gunzip -c /var/lib/monitor/backups/monitor-<ts>.db.gz \
  > /var/lib/monitor/monitor.db
sudo systemctl start monitor.service
```

## Rollback to a previous commit

```bash
cd ~/monitor-src
git checkout <known-good-sha>
sudo ./deploy/install.sh
sudo systemctl restart monitor.service
```

## Uninstall

```bash
sudo systemctl disable --now monitor.service monitor-health.timer monitor-backup.timer
sudo rm /etc/systemd/system/monitor.service
sudo rm /etc/systemd/system/monitor-health.{service,timer}
sudo rm /etc/systemd/system/monitor-backup.{service,timer}
sudo systemctl daemon-reload
sudo rm /etc/logrotate.d/monitor
# Data deletion is deliberate and manual — do these only if you really mean it:
# sudo rm -rf /opt/monitor /var/lib/monitor /var/log/monitor /etc/monitor
# sudo userdel monitor
```

## Security posture

- `monitor` is a non-login system user (`/usr/sbin/nologin`).
- Service unit uses `ProtectSystem=strict`, `ProtectHome=true`, `NoNewPrivileges`,
  `MemoryDenyWriteExecute`, and writable-path allowlist. Only
  `/var/lib/monitor` and `/var/log/monitor` are writable at runtime.
- Secrets live in `/etc/monitor/monitor.env` (mode 640, root:monitor). The
  daemon reads them via `EnvironmentFile` — no secrets on the process
  command line or in the repo.
- Health and backup units reuse the same user; health has the DB mounted
  read-only via `ReadOnlyPaths`.
