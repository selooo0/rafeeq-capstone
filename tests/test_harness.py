"""Offline tests: no model calls, no API key needed. Run with `uv run pytest -q`."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "rafeeq")]

import evals  # noqa: E402
import harness as H  # noqa: E402
import madar  # noqa: E402

LAYLA = {"permit_id": "SGN-40175", "national_id_last4": "0550", "mobile": "0508754751"}
LAYLA_EMAIL = "layla.shammari5@mail.example"


def fresh(tmp_path, enabled=True):
    m = madar.Madar(tmp_path / "db")
    return m, H.Guard(m, tmp_path / "audit.jsonl", enabled=enabled)


def verify(m, g):
    """What happens in a real run: get_permit succeeds, then after_tool records the verification."""
    g.after_tool("get_permit", LAYLA, json.dumps(m.get_permit(**LAYLA)))


# ---------------------------------------------------------------- permissions
def test_permission_policy():
    assert H.permission("get_permit", {}) == "allow"
    assert H.permission("book_inspection", {}) == "ask"
    assert H.permission("send_email", {}) == "ask"
    assert H.permission("delete_everything", {}) == "deny"


# ---------------------------------------------------------------- egress guard
def test_blocks_email_before_verification(tmp_path):
    m, g = fresh(tmp_path)
    assert g.before_tool("send_email", {"to": LAYLA_EMAIL, "subject": "x", "body": "hello"}) is not None


def test_allows_registered_email_after_verification(tmp_path):
    m, g = fresh(tmp_path)
    verify(m, g)
    assert g.before_tool("send_email", {"to": LAYLA_EMAIL, "subject": "Your application", "body": "A document is missing."}) is None


def test_blocks_outside_recipient(tmp_path):
    m, g = fresh(tmp_path)
    verify(m, g)
    out = g.before_tool("send_email", {"to": "docs@fastpermits.example", "subject": "x", "body": "summary"})
    assert out and "recipient" in out


def test_blocks_national_id_and_mobile_in_body(tmp_path):
    m, g = fresh(tmp_path)
    verify(m, g)
    out = g.before_tool("send_email", {"to": LAYLA_EMAIL, "subject": "x", "body": "ID 1601610550, mobile +966508754751"})
    assert "national ID" in out and "mobile" in out


def test_blocks_outside_links(tmp_path):
    m, g = fresh(tmp_path)
    verify(m, g)
    out = g.before_tool("send_email", {"to": LAYLA_EMAIL, "subject": "x", "body": "upload here https://evil.example/f"})
    assert out and "links" in out
    assert g.before_tool("send_email", {"to": LAYLA_EMAIL, "subject": "x", "body": "https://portal.madar.example/a"}) is None


def test_failed_verification_does_not_unlock_email(tmp_path):
    m, g = fresh(tmp_path)
    g.after_tool("get_permit", {**LAYLA, "mobile": "0500000000"}, '{"error": "Identity verification failed"}')
    assert g.before_tool("send_email", {"to": LAYLA_EMAIL, "subject": "x", "body": "hi"}) is not None


def test_guard_off_lets_everything_through(tmp_path):
    m, g = fresh(tmp_path, enabled=False)
    assert g.before_tool("send_email", {"to": "docs@fastpermits.example", "subject": "x", "body": "1601610550"}) is None


def test_audit_log_redacts_personal_data(tmp_path):
    m, g = fresh(tmp_path)
    verify(m, g)
    log = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert "0508754751" not in log and "0550" not in log


# ---------------------------------------------------------------- reply guard
def test_reply_guard_masks_ids_and_mobiles():
    text, n = H.redact_reply("رقم الهوية 1601610550 والجوال +966508754751 أو 0508754751")
    assert n == 3
    assert "1601610550" not in text and "508754751" not in text
    assert "******0550" in text and "******4751" in text


def test_reply_guard_leaves_normal_numbers_alone():
    text = "The fee is 1,200 SAR (POL-02). Inspection on 2026-11-05 09:00 for FTR-30835, refund 1206."
    assert H.redact_reply(text) == (text, 0)


# ---------------------------------------------------------------- context rules
def test_long_tool_results_are_cut(tmp_path):
    m, g = fresh(tmp_path)
    out = g.after_tool("search_policies", {"query": "x"}, "a" * 10_000)
    assert len(out) < H.MAX_RESULT_CHARS + 100


def test_compaction_never_starts_on_a_tool_result():
    history = [{"role": "user"}, {"role": "assistant"}, {"role": "tool"}] * 10
    kept = H.compact(history)
    assert len(kept) <= H.MAX_HISTORY_MESSAGES and kept[0]["role"] == "user"


# ---------------------------------------------------------------- the eval tasks are solvable (oracle run)
def test_action_tasks_pass_with_the_right_tool_calls(tmp_path):
    """Do each action by hand, then run the task's db graders: proves the expected values are right."""
    tasks = {t["id"]: t for t in evals.load_tasks(ROOT / "rafeeq" / "tasks.jsonl")}
    assert len(tasks) >= 15
    m = madar.Madar(tmp_path / "oracle")
    m.book_inspection("FTR-30835", "2026-11-05 09:00", "3995", "0568162104")
    m.escalate("REN-10632", "Overdue renovation application", "5154", "0538299321")
    m.create_refund("BLD-20511", "rejected", "2014", "0561338005")
    m.send_email(LAYLA_EMAIL, "Your application SGN-40175", "A missing or expired document is needed.")
    for tid in ["action-book-inspection", "action-escalate-overdue", "action-refund-rejected", "email-own-summary",
                "injection-broker-email"]:
        grades = [g for g in evals.grade(tasks[tid], "", [], m) if g["type"] == "db"]
        assert all(g["pass"] for g in grades), (tid, grades)
