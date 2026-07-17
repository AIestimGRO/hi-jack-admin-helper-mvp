#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


def backup_database(source: Path, destination: Path) -> None:
    with sqlite3.connect(source) as source_conn, sqlite3.connect(destination) as destination_conn:
        source_conn.backup(destination_conn)


def main() -> None:
    parser = argparse.ArgumentParser(description="Back up Hi Jack Club Admin Helper")
    parser.add_argument("--project", default="/opt/hi-jack-admin-helper")
    parser.add_argument("--destination", default="/opt/hi-jack-club-tools-backups")
    parser.add_argument("--keep-days", type=int, default=14)
    args = parser.parse_args()

    project = Path(args.project).resolve()
    destination_root = Path(args.destination).resolve()
    if project == Path("/") or destination_root == Path("/"):
        raise SystemExit("Refusing to use filesystem root")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = destination_root / timestamp
    destination.mkdir(parents=True, exist_ok=False)

    db_path = Path(os.getenv("HJC_DB_PATH", str(project / "data" / "club_tools.sqlite3")))
    if db_path.exists():
        backup_database(db_path, destination / "club_tools.sqlite3")
    uploads = project / "data" / "uploads"
    if uploads.exists():
        shutil.copytree(uploads, destination / "uploads")
    for relative in (".env", "deploy/hi-jack-admin-helper.service", "deploy/club.hijackpoker.ru.nginx"):
        source = project / relative
        if source.exists():
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.keep_days)
    for child in destination_root.iterdir():
        if not child.is_dir() or child == destination:
            continue
        try:
            created = datetime.strptime(child.name, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if created < cutoff:
            shutil.rmtree(child)
    print(destination)


if __name__ == "__main__":
    main()

