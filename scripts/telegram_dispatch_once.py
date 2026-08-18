from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import Settings  # noqa: E402
from app.telegram_transport import dispatch_telegram_outbox_once  # noqa: E402


def main() -> int:
    settings = Settings()
    settings.validate()
    result = dispatch_telegram_outbox_once(settings, limit=20)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    sys.exit(main())
