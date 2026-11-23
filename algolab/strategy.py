"""Strategy plug-in interface. Drop a file in algolab/strategies/ and it appears in the dashboard."""
from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass
from datetime import time
from types import SimpleNamespace


@dataclass
class Param:
    default: object
    label: str
    min: float | None = None
    max: float | None = None
    choices: list | None = None


def parse_time(text: str) -> time:
    h, m = str(text).strip().split(":")[:2]
    return time(int(h), int(m))


class Strategy:
    """Base class. Override on_bar(); keep anything that must survive a restart in get_state()."""
    name = ""
    title = ""
    description = ""
    PARAMS: dict[str, Param] = {}
    live_ready = False   # True once a strategy has been checked for real orders (live mode)

    def __init__(self, **params):
        values = {k: p.default for k, p in self.PARAMS.items()}
        for k, v in params.items():
            if k not in values:
                raise ValueError(f"Unknown parameter '{k}' for strategy '{self.name}'")
            default = values[k]
            if isinstance(default, bool):
                values[k] = v if isinstance(v, bool) else str(v).strip().lower() not in ("", "0", "false", "no", "off")
            else:
                values[k] = type(default)(v)
        self.p = SimpleNamespace(**values)

    @property
    def params(self) -> dict:
        return dict(vars(self.p))

    def on_bar(self, ctx) -> None:  # called once per bar / live poll
        raise NotImplementedError

    def on_outside_close(self, ctx, contract, qty: int) -> None:
        """Live: `qty` of `contract` was squared off outside Algo Lab (by hand in Kite) and is booked closed.
        The other legs are kept. The default needs nothing: ctx.close() only closes what is held. A strategy that
        re-enters or rolls a leg overrides this so the leg Hamid closed is not traded again in that trade."""

    def on_squared_off(self, ctx, contract, qty: int, reason: str) -> None:
        """The session squared off a leg itself (Stop on the card, a halt): the strategy forgets it as open. By default
        as a leg closed in Kite (on_outside_close)."""
        self.on_outside_close(ctx, contract, qty)

    def fresh_cycle(self, today: str) -> str | None:
        """Resumed from the dashboard on the same day with nothing open (after Stop): forget today's cycle so a new
        one starts at the next bar, within the entry window and with the entry checks. Returns a line for the log,
        or None if the strategy has nothing to forget (it then carries on as it was). The default: nothing."""
        return None

    def recheck_today(self, today: str) -> str | None:
        """Forget today's "no trade today" decision so the entry filters are checked again at the next bar (Re-check
        on the card). Returns what was cleared, or None if there was nothing to clear. The default: nothing."""
        return None

    def get_state(self) -> dict:
        return {}

    def status(self, ctx) -> dict | None:
        """Optional, for the Replay tab: {"pnl", "target", "stop", "levels": {name: index level}, "note"}.
        Rupee amounts are for the current cycle; any key may be left out."""
        return None

    def set_state(self, state: dict) -> None:
        pass


def load_strategies() -> dict[str, type[Strategy]]:
    from . import strategies as pkg
    found: dict[str, type[Strategy]] = {}
    for mod in pkgutil.iter_modules(pkg.__path__):
        if mod.name.startswith("_"):
            continue
        module = importlib.import_module(f"{pkg.__name__}.{mod.name}")
        for obj in vars(module).values():
            if isinstance(obj, type) and issubclass(obj, Strategy) and obj is not Strategy and obj.name:
                found[obj.name] = obj
    try:                                     # made in Algo Studio's Strategy Developer (algolab/built.py)
        from .built import classes
        found.update(classes())
    except Exception:  # noqa: BLE001  a broken spec never hides the built-in strategies
        pass
    return dict(sorted(found.items(), key=lambda kv: kv[0] != "short_straddle"))
