from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import Settings  # noqa: E402
from app.telegram_scheduler import run_scheduled_delivery_once  # noqa: E402


def main() -> int:
    settings = Settings()
    settings.validate()
    result = run_scheduled_delivery_once(settings)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
