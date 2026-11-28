"""Stop a backtest or sweep that is running: Algo Lab › Run / Sweep › Stop.

The page writes a stop file for the job (server: runtime/backtest-stop/<id>; the Mac helper makes its own copy
when the server tells it). The runner names that file in $ALGOLAB_STOP_FILE, so sweep worker processes see it
too, and the engine looks for it once a trading day: the job then ends with status "stopped"."""
from __future__ import annotations

import os
from pathlib import Path

ENV = "ALGOLAB_STOP_FILE"


class Stopped(Exception):
    """Asked to stop on the page."""


def watch(path: Path | str | None) -> None:
    """Look for `path` from now on (None: stop looking). Set before worker processes start: they inherit it."""
    if path:
        os.environ[ENV] = str(path)
    else:
        os.environ.pop(ENV, None)


def asked() -> bool:
    p = os.environ.get(ENV)
    return bool(p) and os.path.exists(p)


def check() -> None:
    if asked():
        raise Stopped("Stopped on the page")
