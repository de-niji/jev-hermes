"""jev — TypeSafe Jev for Hermes Agent.

Install: ``hermes plugins install de-niji/jev-hermes --enable``. Registers the ``jev`` toolset
(``jev_decide``, ``jev_mail_triage``), the risky-command gate (``pre_tool_call`` on ``terminal``),
the bundled skill as ``jev:jev`` and, when ``context.engine: jev`` is set, the Jev context engine.
"""
from __future__ import annotations

import logging
from pathlib import Path

from . import hermes_gate as _gate
from . import hermes_tools as _t

logger = logging.getLogger("jev")


def _context_engine_selected() -> bool:
    # Hermes keeps ONE plugin context engine; only claim the slot when the user picked ours.
    try:
        from hermes_cli.config import load_config_readonly
        ctx_cfg = (load_config_readonly() or {}).get("context") or {}
        return str(ctx_cfg.get("engine") or "").strip().lower() == "jev"
    except Exception:
        return False


def _settings_reader(ctx):
    """ctx.get_config, or the same lookup by hand for Hermes' minimal context-engine loader ctx."""
    get_config = getattr(ctx, "get_config", None)
    if callable(get_config):
        return get_config

    def read(key: str, default=None):
        try:
            from hermes_cli.config import load_config_readonly
            node = (((load_config_readonly() or {}).get("plugins") or {}).get("entries") or {}).get("jev") or {}
            node = node.get("settings") or {}
            for part in key.split("."):
                node = node.get(part) if isinstance(node, dict) else None
            return default if node is None else node
        except Exception:
            return default
    return read


def register(ctx) -> None:
    get_config = _settings_reader(ctx)
    for name, schema, handler, emoji in _t.TOOLS:
        ctx.register_tool(name=name, toolset=_t.TOOLSET, schema=schema, handler=handler,
                          check_fn=_t.available, emoji=emoji)
    ctx.register_hook("pre_tool_call", _gate.Gate(get_config))
    if hasattr(ctx, "register_skill"):
        ctx.register_skill("jev", Path(__file__).resolve().parent / "SKILL.md",
                           description="When and how to use Jev for routing, gating and mail triage.")
    if _context_engine_selected():
        try:
            from . import hermes_context as _context
            ctx.register_context_engine(_context.make_engine(get_config))
        except Exception:
            logger.exception("jev: context engine unavailable; Hermes falls back to its built-in compressor")
