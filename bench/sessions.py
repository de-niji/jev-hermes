"""Labelled synthetic agent sessions for the compaction benchmark.

Every tool output carries a ground-truth label as of the END of the session:
``needed`` = the assistant still needs it verbatim to finish the current task;
``stale`` = superseded (re-read, re-run, fixed) or about a topic the user has left.
Labels live beside the messages (``labels``), never inside them, so Jev cannot see them.
"""
from __future__ import annotations

import json
from typing import Any


def _lines(prefix: str, n: int) -> str:
    return "\n".join(f"{prefix} {i}" for i in range(n))


class Builder:
    def __init__(self, system: str):
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        self.labels: dict[str, str] = {}
        self._n = 0

    def user(self, text: str) -> "Builder":
        self.messages.append({"role": "user", "content": text})
        return self

    def say(self, text: str) -> "Builder":
        self.messages.append({"role": "assistant", "content": text})
        return self

    def tool(self, name: str, args: dict, output: str, label: str) -> "Builder":
        assert label in ("needed", "stale")
        self._n += 1
        cid = f"call_{self._n}"
        self.messages.append({"role": "assistant", "content": None, "tool_calls": [
            {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]})
        self.messages.append({"role": "tool", "tool_call_id": cid, "content": output})
        self.labels[cid] = label
        return self


def bugfix() -> Builder:
    b = Builder("You are a coding agent working in a Python repo.")
    b.user("The test test_invoice_total fails. Please fix it.")
    b.tool("terminal", {"command": "pytest tests/test_invoice.py -q"},
           "F.\n=== FAILURES ===\ntest_invoice_total: assert 107.0 == 119.0\n"
           "invoice.py:42 total = net + net * 0.07\n" + _lines("  traceback frame", 40), "stale")
    b.tool("read_file", {"path": "invoice.py"},
           "def total(net, vat=0.19):\n    # line 42\n    return net + net * 0.07  # BUG: ignores vat\n"
           + _lines("# helper code line", 60), "stale")
    b.tool("terminal", {"command": "grep -rn 'total(' --include=*.py ."},
           "invoice.py:40:def total(net, vat=0.19):\nreport.py:12:    t = total(net)\n"
           "api.py:88:    return total(net, vat=rate)\n" + _lines("vendor/lib.py: total(", 30), "stale")
    b.say("The function ignores the vat argument. I'll use it.")
    b.tool("patch", {"path": "invoice.py", "diff": "-    return net + net * 0.07\n+    return net + net * vat"},
           "Patched invoice.py (1 hunk)\n" + _lines("context line", 20), "stale")
    b.tool("terminal", {"command": "pytest tests/test_invoice.py -q"},
           "..\n2 passed in 0.12s\n" + _lines("  collected item", 10), "stale")
    b.say("Fixed: total() now uses the vat argument; the tests pass.")
    b.user("Great. Now also make report.py pass the customer's VAT rate instead of the default.")
    b.tool("read_file", {"path": "report.py"},
           "from invoice import total\n\ndef build(customer, net):\n    # line 12\n    t = total(net)\n"
           "    return {'customer': customer.id, 'total': t, 'vat_rate': customer.vat_rate}\n"
           + _lines("# report helper line", 50), "needed")
    b.tool("read_file", {"path": "models/customer.py"},
           "class Customer:\n    id: str\n    vat_rate: float  # e.g. 0.19 or 0.07\n    country: str\n"
           + _lines("# model field doc line", 40), "needed")
    b.tool("terminal", {"command": "git log --oneline -5"},
           "a1b2c3 fix vat in total()\n" + _lines("older commit", 4) + "\n" + _lines("  log detail", 30), "stale")
    return b


def ops() -> Builder:
    b = Builder("You are an ops agent on a Linux server.")
    b.user("The web service is slow since this morning. Can you check?")
    b.tool("terminal", {"command": "df -h"},
           "Filesystem Size Used Avail Use% Mounted on\n/dev/sda1 100G 41G 59G 41% /\n"
           + _lines("tmpfs line", 20), "stale")
    b.tool("terminal", {"command": "systemctl status web"},
           "web.service - Web\n Active: active (running) since 07:02\n Memory: 3.9G (limit 4.0G)\n"
           + _lines(" log: request handled", 40), "stale")
    b.tool("terminal", {"command": "journalctl -u web --since today | tail -n 200"},
           _lines("INFO request ok", 60) + "\nWARN memory pressure: 3.9G/4.0G, GC thrashing\n"
           + "ERROR worker 3 killed by OOM at 11:47\n" + _lines("INFO request ok", 60), "needed")
    b.say("The service is near its 4G memory limit and a worker was OOM-killed. A restart should help "
          "short-term; the root cause looks like a leak.")
    b.user("Restart it, then find what leaks.")
    b.tool("terminal", {"command": "systemctl restart web && systemctl status web"},
           "web.service - Web\n Active: active (running) since 12:03\n Memory: 610M (limit 4.0G)\n"
           + _lines(" log: startup", 30), "needed")
    b.tool("terminal", {"command": "ls /var/log/web/"},
           _lines("access.log.", 40), "stale")
    b.tool("read_file", {"path": "/etc/web/config.yaml"},
           "cache:\n  enabled: true\n  max_entries: 0   # unbounded!\n  ttl: 0\nworkers: 4\n"
           + _lines("# config comment", 40), "needed")
    return b


def research() -> Builder:
    b = Builder("You are a research assistant with a web_search tool.")
    b.user("What are the opening hours of the city library on Saturdays?")
    b.tool("web_search", {"query": "city library opening hours saturday"},
           "Result 1: City Library - Sat 10:00-14:00 (main branch)\nResult 2: Branch Nord Sat closed\n"
           + _lines("unrelated result snippet", 40), "stale")
    b.say("The main branch is open Saturdays 10:00-14:00; branch Nord is closed.")
    b.user("Is there wifi there?")
    b.tool("web_search", {"query": "city library wifi password"},
           "Library wifi: ask at the front desk.\n" + _lines("library event listing", 30), "stale")
    b.say("Yes, ask at the front desk for the wifi password.")
    b.user("Thanks. Different topic: I need to renew my passport. What documents do I need and what does it cost?")
    b.tool("web_search", {"query": "passport renewal documents required"},
           "Required: old passport, biometric photo (35x45 mm), ID card, appointment confirmation.\n"
           + _lines("forum post about photo sizes", 30), "needed")
    b.tool("web_search", {"query": "passport renewal fee adults"},
           "Fee: 70 EUR for adults (10 years validity), express +32 EUR.\n"
           + _lines("news item unrelated", 30), "needed")
    return b


SESSIONS = {"bugfix": bugfix, "ops": ops, "research": research}
