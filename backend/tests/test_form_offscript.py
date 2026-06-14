"""Tests for logical handling of off-script input during ticket-form collection.

Reproduces the bug: while the bot asks for a phone number, the customer replies
with a question/objection ("kenapa butuh nomor HP saya?!"). The bot must explain
WHY and re-ask — not reject it as an invalid phone format, and not store the
question as the value.
"""
import time

import agent


def _session_in(state: str) -> str:
    agent.SESSION_STATE.clear()
    cid = "2001"
    s = agent._fresh_session()
    s.update({"state": state, "last_activity": time.time(), "ticket_form": {},
              "chat_history": [{"role": "assistant", "text": "Berapa nomor HP Anda?", "ts": time.time()}]})
    agent.SESSION_STATE[cid] = s
    return cid


def test_question_during_phone_explains_and_reasks_not_invalid_format():
    cid = _session_in("COLLECTING_PHONE")
    resp = agent.process_message(cid, "why do you need my phone number?!")

    assert "Format nomor HP tidak valid" not in resp["text"]
    assert "tidak dibagikan" in resp["text"]            # explains the reason
    assert "nomor hp" in resp["text"].lower()           # re-asks for it
    s = agent.SESSION_STATE[cid]
    assert s["state"] == "COLLECTING_PHONE"             # stayed on the same field
    assert "phone" not in s["ticket_form"]              # did NOT store the question


def test_refusal_during_phone_is_handled():
    cid = _session_in("COLLECTING_PHONE")
    resp = agent.process_message(cid, "ga mau kasih nomor")
    assert agent.SESSION_STATE[cid]["state"] == "COLLECTING_PHONE"
    assert "nomor hp" in resp["text"].lower()


def test_question_during_name_does_not_become_the_name():
    cid = _session_in("COLLECTING_NAME")
    resp = agent.process_message(cid, "kenapa kamu butuh nama saya?")
    s = agent.SESSION_STATE[cid]
    assert s["state"] == "COLLECTING_NAME"
    assert s["ticket_form"].get("name") != "kenapa kamu butuh nama saya?"
    assert "nama" in resp["text"].lower()


def test_valid_phone_still_advances():
    cid = _session_in("COLLECTING_PHONE")
    resp = agent.process_message(cid, "081234567890")
    s = agent.SESSION_STATE[cid]
    assert s["ticket_form"]["phone"] == "081234567890"
    assert s["state"] == "COLLECTING_COMPANY"
    assert "perusahaan" in resp["text"].lower()


def test_phone_extracted_from_a_noisy_compliant_reply():
    cid = _session_in("COLLECTING_PHONE")
    resp = agent.process_message(cid, "oh oke, ini +62 812-3456-7890")
    s = agent.SESSION_STATE[cid]
    assert s["ticket_form"]["phone"] == "+6281234567890"
    assert s["state"] == "COLLECTING_COMPANY"


def test_real_name_is_not_mistaken_for_a_question():
    cid = _session_in("COLLECTING_NAME")
    agent.process_message(cid, "Budi Santoso")
    s = agent.SESSION_STATE[cid]
    assert s["ticket_form"]["name"] == "Budi Santoso"
    assert s["state"] == "COLLECTING_PHONE"


def test_detector_unit():
    assert agent._looks_like_question_or_refusal("kenapa?") is True
    assert agent._looks_like_question_or_refusal("buat apa sih") is True
    assert agent._looks_like_question_or_refusal("ga mau") is True
    assert agent._looks_like_question_or_refusal("Budi Santoso") is False
    assert agent._looks_like_question_or_refusal("Kopi Kenangan") is False
    assert agent._looks_like_question_or_refusal("081234567890") is False
