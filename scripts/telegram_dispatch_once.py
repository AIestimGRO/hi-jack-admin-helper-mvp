from __future__ import annotations

import json
import sys

from app.config import Settings
from app.telegram_transport import dispatch_telegram_outbox_once


def main() -> int:
    settings = Settings()
    settings.validate()
    result = dispatch_telegram_outbox_once(settings, limit=20)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    sys.exit(main())
