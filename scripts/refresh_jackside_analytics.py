from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import transaction
from app.services.jackside_analytics import refresh_jackside_analytics


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Refresh cached JACKSIDE ratings and analytics."
    )
    parser.add_argument("db_path", type=Path)
    parser.add_argument("--timezone", default="Europe/Moscow")
    args = parser.parse_args()
    with transaction(args.db_path) as conn:
        payload = refresh_jackside_analytics(
            conn, timezone_name=args.timezone
        )
    print(payload["generated_at"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
