"""The harness around Rafeeq: permissions, the egress guard, budgets, the audit log and the compaction rule.

Everything in this file is plain code. None of it depends on the model following instructions:
the model can *try* any tool call, and the harness decides what actually happens.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import madar

# ---------------------------------------------------------------- budgets (requirement 2 and 3)
MAX_STEPS = 6             # model turns per user message; a normal task needs 2-4
MAX_COST = 0.05           # USD per user message (free models cost 0, this protects a paid fallback)
MAX_RESULT_CHARS = 2500   # a tool result longer than this is cut before it enters the context
MAX_HISTORY_MESSAGES = 20 # chat mode keeps only the most recent messages (compaction rule)

# ---------------------------------------------------------------- permissions (requirement 3)
READ_TOOLS = {"search_policies", "get_permit", "compute_fee", "list_slots"}
WRITE_TOOLS = {"book_inspection", "reschedule_inspection", "create_refund", "escalate", "send_email"}


def permission(name: str, args: dict) -> str:
    """Reads run freely, writes need a human yes, anything unknown is denied."""
    if name in READ_TOOLS:
        return "allow"
    if name in WRITE_TOOLS:
        return "ask"
    return "deny"


def approve_cli(name: str, args: dict) -> bool:
    """The human approval step: show the exact arguments, then wait for y/N."""
    shown = json.dumps(args, ensure_ascii=False, indent=2)
    return input(f"\n🛡️  Rafeeq wants to run {name} with:\n{shown}\nApprove? [y/N] ").strip().lower() == "y"


# ---------------------------------------------------------------- egress guard (security defence)
ID_RE = re.compile(r"(?<!\d)[12]\d{9}(?!\d)")             # a Saudi national ID / iqama: 10 digits starting 1 or 2
MOBILE_RE = re.compile(r"(?<!\d)(?:\+?966|0)?5\d{8}(?!\d)")  # a Saudi mobile: 05XXXXXXXX, 9665XXXXXXXX, +9665...
LINK_RE = re.compile(r"https?://([^/\s]+)")
ALLOWED_LINK_DOMAIN = "madar.example"


class Guard:
    """Per-run harness state. One Guard per user message (or per chat session).

    It remembers which permits were verified in this run, so the egress guard can check that an email goes
    only to the registered address of a verified applicant.
    """

    def __init__(self, m: madar.Madar, audit_path: Path, enabled: bool = True):
        self.m = m
        self.audit_path = audit_path
        self.enabled = enabled            # False only for the attack demo ("before the defence")
        self.allowed_recipients: set[str] = set()
        self.blocked: list[dict] = []

    # -- called by the agent loop BEFORE a tool runs; returning a string blocks the call
    def before_tool(self, name: str, args: dict) -> str | None:
        if name != "send_email" or not self.enabled:
            return None
        reasons = self.email_problems(args)
        if not reasons:
            return None
        self.blocked.append({"tool": name, "args": args, "reasons": reasons})
        self._log({"event": "blocked", "tool": name, "to": args.get("to"), "reasons": reasons})
        return json.dumps({"error": "Blocked by the harness: " + "; ".join(reasons)
                           + ". Tell the person the email was not sent and why."}, ensure_ascii=False)

    def email_problems(self, args: dict) -> list[str]:
        """The egress rules for send_email. Returns the reasons to block (empty list = allowed)."""
        to = str(args.get("to", "")).strip().lower()
        body = f"{args.get('subject', '')}\n{args.get('body', '')}"
        reasons = []
        if to not in self.allowed_recipients:
            reasons.append("recipient is not the registered email of an applicant verified in this conversation")
        if ID_RE.search(body):
            reasons.append("the email contains a national ID number")
        if MOBILE_RE.search(body):
            reasons.append("the email contains a mobile number")
        if any(not host.lower().endswith(ALLOWED_LINK_DOMAIN) for host in LINK_RE.findall(body)):
            reasons.append(f"links are allowed only to {ALLOWED_LINK_DOMAIN}")
        return reasons

    # -- called by the agent loop AFTER a tool runs; may replace the result
    def after_tool(self, name: str, args: dict, result: str) -> str | None:
        ok = not result.startswith('{"error"')
        self._log({"event": "tool", "tool": name, "args": _redact(args), "ok": ok, "chars": len(result)})
        if ok and args.get("permit_id") and args.get("national_id_last4"):
            self._remember_verified(args["permit_id"])  # the tool already checked ID + mobile (POL-08)
        if len(result) > MAX_RESULT_CHARS:
            return result[:MAX_RESULT_CHARS] + f"\n[… {len(result) - MAX_RESULT_CHARS} characters cut; ask for less]"
        return None

    def _remember_verified(self, permit_id: str) -> None:
        rows = self.m.sql("SELECT a.email FROM permits p JOIN applicants a USING (applicant_id) WHERE p.permit_id = ?",
                          (str(permit_id).strip().upper(),))
        if rows:
            self.allowed_recipients.add(rows[0]["email"].lower())

    def _log(self, entry: dict) -> None:
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _redact(args: dict) -> dict:
    """The audit log keeps what happened, not the personal data (PDPL data minimisation)."""
    hidden = {"mobile", "national_id_last4", "body"}
    return {k: ("***" if k in hidden else v) for k, v in args.items()}


# ---------------------------------------------------------------- reply guard (data minimisation, PDPL)
def redact_reply(text: str) -> tuple[str, int]:
    """Mask any full national ID or mobile number in Rafeeq's reply before the person sees it.

    The egress guard protects what leaves by email; this protects what is shown on screen (a shared screen,
    a forwarded chat, a log). Only the last 4 digits stay, as on Madar's own records.
    """
    count = 0

    def mask(match: re.Match) -> str:
        nonlocal count
        count += 1
        digits = re.sub(r"\D", "", match.group(0))
        return "******" + digits[-4:]

    text = ID_RE.sub(mask, text or "")
    text = MOBILE_RE.sub(mask, text)
    return text, count


# ---------------------------------------------------------------- compaction (context rule)
def compact(history: list) -> list:
    """Keep the last MAX_HISTORY_MESSAGES messages, starting on a user message (never on a tool result)."""
    if len(history) <= MAX_HISTORY_MESSAGES:
        return history
    tail = history[-MAX_HISTORY_MESSAGES:]
    while tail and tail[0].get("role") != "user":
        tail = tail[1:]
    return tail
