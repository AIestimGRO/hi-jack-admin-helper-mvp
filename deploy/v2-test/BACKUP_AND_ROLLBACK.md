# Backup and rollback (v2 test)

Safe steps before pulling launch-hardening (or any schema-touching branch) onto
the parallel v2 host. Does **not** change the production
`/opt/hi-jack-admin-helper` tree.

Paths assume the layout from [README.md](README.md):

- Project: `/opt/hi-jack-admin-helper-v2`
- DB: `/opt/hi-jack-admin-helper-v2/data/club_tools.sqlite3`
- Service: `hi-jack-admin-helper-v2`
- Port: `127.0.0.1:8091`

## 1. Record current commit

```bash
cd /opt/hi-jack-admin-helper-v2
sudo -u www-data git rev-parse HEAD
sudo -u www-data git status -sb
```

Save the SHA (example: `PREV_SHA`).

## 2. Backup SQLite before pull

Prefer the project backup script (includes DB via online backup API):

```bash
sudo -u www-data /opt/hi-jack-admin-helper-v2/.venv/bin/python \
  /opt/hi-jack-admin-helper-v2/scripts/backup.py \
  --project /opt/hi-jack-admin-helper-v2 \
  --destination /opt/hi-jack-club-tools-backups/v2-pre-pull
```

Or a one-shot dated copy while the service may stay up:

```bash
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
sudo install -d -o www-data -g www-data "/opt/hi-jack-club-tools-backups/v2-manual/$STAMP"
sudo -u www-data python3 -c "
import sqlite3
src='/opt/hi-jack-admin-helper-v2/data/club_tools.sqlite3'
dst='/opt/hi-jack-club-tools-backups/v2-manual/$STAMP/club_tools.sqlite3'
s=sqlite3.connect(src); d=sqlite3.connect(dst); s.backup(d); d.close(); s.close()
print(dst)
"
```

Keep the printed backup path for rollback.

## 3. Pull / checkout new code

```bash
cd /opt/hi-jack-admin-helper-v2
sudo systemctl stop hi-jack-admin-helper-v2
sudo -u www-data git fetch origin
sudo -u www-data git checkout agent/jackside-public-launch-hardening
# or: sudo -u www-data git pull --ff-only
sudo -u www-data .venv/bin/pip install -r requirements.txt
sudo systemctl start hi-jack-admin-helper-v2
curl --fail -sS http://127.0.0.1:8091/health
```

Confirm `status` is `ok` and `schema_version` is present.

## 4. Rollback to previous commit

```bash
cd /opt/hi-jack-admin-helper-v2
sudo systemctl stop hi-jack-admin-helper-v2
sudo -u www-data git checkout "$PREV_SHA"
sudo -u www-data .venv/bin/pip install -r requirements.txt
```

## 5. Restore SQLite backup

With the service **stopped**:

```bash
BACKUP=/opt/hi-jack-club-tools-backups/v2-manual/<STAMP>/club_tools.sqlite3
sudo -u www-data rm -f \
  /opt/hi-jack-admin-helper-v2/data/club_tools.sqlite3 \
  /opt/hi-jack-admin-helper-v2/data/club_tools.sqlite3-wal \
  /opt/hi-jack-admin-helper-v2/data/club_tools.sqlite3-shm
sudo -u www-data cp "$BACKUP" /opt/hi-jack-admin-helper-v2/data/club_tools.sqlite3
sudo systemctl start hi-jack-admin-helper-v2
curl --fail -sS http://127.0.0.1:8091/health
```

If you used `scripts/backup.py`, restore the `club_tools.sqlite3` file from that
run’s destination directory the same way.

## 6. Smoke after rollback

- `systemctl is-active hi-jack-admin-helper-v2`
- `curl http://127.0.0.1:8091/health` → `status=ok`
- Optional: open quiz host and start/finish one attempt on a non-prod campaign
