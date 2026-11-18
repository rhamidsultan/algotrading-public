"""Phone notifications from the engine: a message dropped into runtime/push_outbox/ is sent by the
portfolio dashboard (pwa_panel) to every device that turned notifications on for its category
(trading, daily, logins, alerts). Never raises: a notification must not disturb trading."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .config import RUNTIME


def push(title: str, body: str, url: str = "/paper", category: str = "trading") -> None:
    try:
        out = Path(os.environ["ALGOLAB_PUSH_DIR"]) if os.environ.get("ALGOLAB_PUSH_DIR") else RUNTIME / "push_outbox"
        out.mkdir(parents=True, exist_ok=True)
        tmp = out / f".{time.time():.6f}-{os.getpid()}.tmp"
        tmp.write_text(json.dumps({"title": str(title)[:120], "body": str(body)[:400], "url": url,
                                   "category": category}), encoding="utf-8")
        tmp.replace(tmp.with_name(tmp.name[1:-4] + ".json"))
    except Exception:  # noqa: BLE001
        pass
