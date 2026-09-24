"""Jev context engine for Hermes: relevance-based tool-output pruning before any LLM summary.

Activate with ``context.engine: jev`` in config.yaml (Hermes never switches engines on its own).

The engine IS Hermes' built-in ContextCompressor with one step swapped: before Hermes' own
no-LLM tool-result prune (``_prune_old_tool_results``) runs, Jev looks at every older tool
output in the context of the whole conversation and answers "does the assistant still need
this verbatim?". Outputs it says no to are replaced by a one-line stub. Nothing else changes:

* no message is removed, so tool calls and results stay paired and Hermes' session store
  sees the same message structure it produces itself;
* user and assistant text is never touched;
* the protected tail (``compression.protect_last_n`` / tail budget) is never touched;
* Hermes' own passes (dedup, size-based summaries, arg truncation) and its LLM summary still
  run afterwards, on a smaller transcript — or not at all when the early prune was enough;
* any Jev error or timeout falls back to exactly the built-in behaviour.

Hermes calls this step in two places: the early prune (``compression.proactive_prune_tokens``;
the engine defaults it to 48000 when you left it at 0) and phase 1 of full compaction.

Settings (``plugins.entries.jev.settings.compact``):
  keep_threshold  0.5    stub an output when Jev's keep-result probability is below this
  min_chars       800    only outputs at least this long are worth a question
  timeout         20     seconds per Jev request (falls back to built-in on timeout)
  prune_tokens    48000  early-prune trigger used when compression.proactive_prune_tokens is 0

Module level stays free of Hermes imports so the offline tests can load it.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Callable, Optional

SCRIPTS = Path(__file__).resolve().parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import jev_compact  # noqa: E402
import jev_decide as jd  # noqa: E402

logger = logging.getLogger("jev.context")

ENGINE_NAME = "jev"
DEFAULTS: dict[str, Any] = {"keep_threshold": 0.5, "min_chars": 800, "timeout": 20.0, "prune_tokens": 48_000}
# Outputs the agent relies on as instructions, not as data: never stubbed.
EXCLUDED_TOOLS = frozenset({"skill_view"})
STUB_PREFIX = "[jev-compact:"


def stub_text(tool: str, text: str) -> str:
    return (f"{STUB_PREFIX} earlier output of {tool} ({len(text)} chars) removed as no longer needed; "
            "re-run the tool if you need it again]")


def _text(content: Any) -> Optional[str]:
    return content if isinstance(content, str) else None


def candidates(messages: list[dict], boundary: int, min_chars: int) -> dict[str, str]:
    """tool_call_id -> tool name for old tool outputs worth asking about.

    Both the call and its result must sit before ``boundary`` (the protected tail starts there).
    """
    call_pos: dict[str, tuple[int, str]] = {}
    for i, m in enumerate(messages[:max(0, boundary)]):
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                if isinstance(tc, dict) and tc.get("id"):
                    call_pos[tc["id"]] = (i, (tc.get("function") or {}).get("name") or "tool")
    out: dict[str, str] = {}
    for m in messages[:max(0, boundary)]:
        if m.get("role") != "tool":
            continue
        cid = m.get("tool_call_id")
        text = _text(m.get("content"))
        if not cid or cid not in call_pos or text is None:
            continue
        tool = call_pos[cid][1]
        if tool in EXCLUDED_TOOLS or len(text) < min_chars or text.startswith(STUB_PREFIX):
            continue
        out[cid] = tool
    return out


def jev_stub(
    messages: list[dict],
    boundary: int,
    score: Callable[[list[dict], set[str]], dict[str, dict[str, float]]],
    *,
    keep_threshold: float,
    min_chars: int,
) -> tuple[list[dict], int]:
    """Replace old tool outputs Jev no longer needs with stubs. Returns (messages, n_stubbed).

    Returns the input list unchanged (same object) when nothing is stubbed. Raises JevError.
    """
    cands = candidates(messages, boundary, min_chars)
    if not cands:
        return messages, 0
    scores = score(messages, set(cands))
    drop = {cid for cid, s in scores.items()
            if cid in cands and float(s.get("keepResult", 1.0)) < keep_threshold}
    if not drop:
        return messages, 0
    out: list[dict] = []
    n = 0
    for m in messages:
        if m.get("role") == "tool" and m.get("tool_call_id") in drop:
            m = m.copy()  # shallow copy, like Hermes' own prune: persistence markers survive
            m["content"] = stub_text(cands[m["tool_call_id"]], m["content"])
            n += 1
        out.append(m)
    return out, n


def settings(get_config: Callable[[str, Any], Any]) -> dict[str, Any]:
    out = dict(DEFAULTS)
    for key, default in DEFAULTS.items():
        try:
            value = get_config(f"compact.{key}", default)
        except Exception:
            value = default
        out[key] = default if value is None else value
    return out


def _compressor_kwargs(cfg: dict, prune_tokens_default: int) -> dict[str, Any]:
    """ContextCompressor kwargs from the ``compression`` block (Hermes only passes them to its own engine)."""
    c = cfg.get("compression") if isinstance(cfg.get("compression"), dict) else {}
    kw: dict[str, Any] = {}
    for key, arg, cast in (
        ("threshold", "threshold_percent", float), ("protect_first_n", "protect_first_n", int),
        ("protect_last_n", "protect_last_n", int), ("target_ratio", "summary_target_ratio", float),
        ("threshold_tokens", "threshold_tokens_cap", int),
        ("proactive_prune_min_result_chars", "proactive_prune_min_result_chars", int),
        ("proactive_prune_min_reclaim_tokens", "proactive_prune_min_reclaim_tokens", int),
        ("min_tail_user_messages", "min_tail_user_messages", int), ("tail_mode", "tail_mode", str),
        ("abort_on_summary_failure", "abort_on_summary_failure", bool),
    ):
        if c.get(key) is not None:
            try:
                kw[arg] = cast(c[key])
            except (TypeError, ValueError):
                pass
    try:
        prune = int(c.get("proactive_prune_tokens") or 0)
    except (TypeError, ValueError):
        prune = 0
    kw["proactive_prune_tokens"] = prune if prune > 0 else int(prune_tokens_default)
    return kw


def make_engine(get_config: Callable[[str, Any], Any], hermes_config: Optional[dict] = None):
    """Build the engine instance (Hermes clones it per agent and calls update_model on the clone)."""
    from agent.context_compressor import ContextCompressor

    opts = settings(get_config)
    if hermes_config is None:
        try:
            from hermes_cli.config import load_config_readonly
            hermes_config = load_config_readonly() or {}
        except Exception:
            hermes_config = {}

    class JevContextEngine(ContextCompressor):
        """ContextCompressor whose no-LLM prune asks Jev which old tool outputs still matter."""

        @property
        def name(self) -> str:
            return ENGINE_NAME

        def _jev_score(self, messages: list[dict], ids: set[str]) -> dict[str, dict[str, float]]:
            return jev_compact.score_calls(
                messages, ids, preserve=self.protect_last_n, timeout=float(self.jev_opts["timeout"]))

        def _prune_old_tool_results(self, messages, protect_tail_count, protect_tail_tokens=None, **kwargs):
            pruned = 0
            try:
                boundary = self._prune_boundary(list(messages), protect_tail_count, protect_tail_tokens)
                messages, pruned = jev_stub(
                    messages, boundary, self._jev_score,
                    keep_threshold=float(self.jev_opts["keep_threshold"]),
                    min_chars=int(self.jev_opts["min_chars"]))
                self.jev_stats["stubbed"] += pruned
            except jd.JevError as e:
                self.jev_stats["errors"] += 1
                logger.warning("jev context engine: Jev unavailable, built-in prune only (%s)", e)
            except Exception:  # never let the engine break compaction
                self.jev_stats["errors"] += 1
                logger.exception("jev context engine: prune failed, built-in prune only")
            messages, count = super()._prune_old_tool_results(
                messages, protect_tail_count, protect_tail_tokens, **kwargs)
            return messages, count + pruned

        def get_status(self) -> dict:
            status = super().get_status()
            status["jev"] = dict(self.jev_stats)
            return status

    engine = JevContextEngine(model="", **_compressor_kwargs(hermes_config, int(opts["prune_tokens"])))
    engine.jev_opts = opts
    engine.jev_stats = {"stubbed": 0, "errors": 0}
    return engine
