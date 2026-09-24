"""jev — TypeSafe Jev for Hermes Agent.

Install: ``hermes plugins install de-niji/jev-hermes --enable``. Registers the ``jev`` toolset
(``jev_decide``, ``jev_mail_triage``), the risky-command gate (``pre_tool_call`` on ``terminal``)
and the bundled skill as ``jev:jev``.
"""
from __future__ import annotations

from pathlib import Path

from . import hermes_gate as _gate
from . import hermes_tools as _t


def register(ctx) -> None:
    for name, schema, handler, emoji in _t.TOOLS:
        ctx.register_tool(name=name, toolset=_t.TOOLSET, schema=schema, handler=handler,
                          check_fn=_t.available, emoji=emoji)
    ctx.register_hook("pre_tool_call", _gate.Gate(ctx.get_config))
    ctx.register_skill("jev", Path(__file__).resolve().parent / "SKILL.md",
                       description="When and how to use Jev for routing, gating and mail triage.")
