"""Risky-command gate: a Hermes ``pre_tool_call`` hook that asks Jev about terminal commands.

Hermes already asks the user before commands its own patterns flag (recursive deletes, piping downloads into a shell,
force pushes, ...). This gate covers what those patterns miss (``kubectl delete ns prod``,
piping secrets to the network, ...): when Jev's ``approval`` preset says ``escalate``, the hook
returns ``{"action": "approve"}`` and Hermes shows its normal human-approval prompt. The gate
never approves anything itself.

Settings (``plugins.entries.jev.settings.gate`` in config.yaml):
  enabled      true   turn the gate off with false
  threshold    0.7    escalate when Jev's escalate probability is at least this
  mode         approve  "approve" asks the user; "block" refuses the command outright
  fail_closed  false  on a Jev error or timeout: false = let Hermes' own checks decide,
                      true = ask the user
  timeout      8      seconds per Jev call (must stay well below plugins.hook_callback_timeout,
                      because a timed-out pre_tool_call hook blocks the tool)

Kept free of Hermes imports at module level so the offline tests can load it.
"""
from __future__ import annotations

import hashlib
import logging
import sys
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Optional

SCRIPTS = Path(__file__).resolve().parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import jev_decide as jd  # noqa: E402

logger = logging.getLogger("jev.gate")

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "threshold": 0.7,
    "mode": "approve",
    "fail_closed": False,
    "timeout": 8.0,
}
CACHE_SIZE = 256


def builtin_flags(command: str) -> bool:
    """True when Hermes' own dangerous-command patterns already catch this command."""
    try:
        from tools.approval_detection import detect_dangerous_command
    except Exception:  # not running inside Hermes, or the API moved: let Jev look at everything
        return False
    try:
        return bool(detect_dangerous_command(command)[0])
    except Exception:
        return False


def _settings(get_config: Callable[[str, Any], Any]) -> dict[str, Any]:
    out = dict(DEFAULTS)
    for key, default in DEFAULTS.items():
        try:
            value = get_config(f"gate.{key}", default)
        except Exception:
            value = default
        out[key] = default if value is None else value
    return out


def _rule_key(command: str) -> str:
    # "[a]lways allow" in Hermes' prompt then applies to this exact command only.
    return "jev:" + hashlib.sha256(command.encode("utf-8")).hexdigest()[:16]


def _escalation(answers: dict) -> tuple[Optional[float], Any]:
    esc = answers.get("escalate") or {}
    risk = (answers.get("risk") or {}).get("score")
    try:
        return float(esc.get("noul")), risk
    except (TypeError, ValueError):
        return None, risk


class Gate:
    def __init__(self, get_config: Callable[[str, Any], Any],
                 decide: Optional[Callable[..., dict]] = None,
                 builtin: Callable[[str], bool] = builtin_flags):
        self._get_config = get_config
        self._decide = decide
        self._builtin = builtin
        self._cache: "OrderedDict[str, tuple[Optional[float], Any]]" = OrderedDict()

    def _ask(self, command: str, timeout: float) -> tuple[Optional[float], Any]:
        if command in self._cache:
            self._cache.move_to_end(command)
            return self._cache[command]
        out = (self._decide or jd.decide)({"cmd": command}, jd.PRESETS["approval"], jd.DEFAULT_MODEL, timeout=timeout)
        result = _escalation(out.get("answers") or {})
        if result[0] is not None:
            self._cache[command] = result
            if len(self._cache) > CACHE_SIZE:
                self._cache.popitem(last=False)
        return result

    def __call__(self, tool_name: str = "", args: Optional[dict] = None, **_: Any) -> Optional[dict]:
        if tool_name != "terminal" or not isinstance(args, dict):
            return None
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            return None
        cfg = _settings(self._get_config)
        if not cfg["enabled"] or self._builtin(command):
            return None
        try:
            p, risk = self._ask(command, float(cfg["timeout"]))
        except jd.JevError as e:
            logger.warning("jev gate: Jev unavailable (%s)", e)
            if cfg["fail_closed"]:
                return {"action": "approve", "rule_key": _rule_key(command),
                        "message": "Jev risk check unavailable; confirm this command"}
            return None
        if p is None or p < float(cfg["threshold"]):
            return None
        message = f"Jev flagged this command as risky (escalate={p:.2f}, risk={risk})"
        if cfg["mode"] == "block":
            return {"action": "block", "message": f"BLOCKED: {message}. Find a safer approach or ask the user."}
        return {"action": "approve", "message": message, "rule_key": _rule_key(command)}
