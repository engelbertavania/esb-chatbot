"""Tests for the define -> predefine -> answer (+ Ya/Tidak/CC) chatbot flow
and the post-ticket out-of-context decline (issues #2, #4, #7)."""
from agent import (
    process_message, SESSION_STATE, _fresh_session,
    HANDOFF_OPTION, TICKET_OPTION, CONFIDENTIAL_DECLINE,
)
from content_architecture import issue_define_options, load_ca


def _seed(chat_id, **over):
    SESSION_STATE[chat_id] = {**_fresh_session(),
                              "chat_history": [{"role": "user", "text": "x", "ts": 0}],
                              **over}


def test_ca_workbook_loads_with_new_column_layout():
    # The V.4 sheet inserted an "Error Define" column A; loader must still parse.
    entries = load_ca()
    assert len(entries) >= 40
    assert all(e["predefined"] and e["response"] for e in entries)
    assert any(e["predefined"] == "Pesanan tidak masuk ke POS" for e in entries)


def test_issue_define_lists_ten_categories():
    opts = issue_define_options()
    assert len(opts) == 10
    labels = [o["label"] for o in opts]
    assert "KENDALA PESANAN" in labels


def test_clear_description_auto_matches_category_to_predefined():
    SESSION_STATE.clear()
    _seed("d1")
    r = process_message("d1", "pesanan tidak masuk ke POS")
    assert r["type"] == "question"
    assert SESSION_STATE["d1"]["state"] == "CHOOSING_PREDEFINED"
    assert "Pesanan tidak masuk ke POS" in r["options"]
    # Layer 2 (predefined list): CC handoff is NOT offered here (only layer 3).
    assert HANDOFF_OPTION not in r["options"]


def test_vague_description_shows_category_menu():
    SESSION_STATE.clear()
    _seed("d2")
    r = process_message("d2", "halo mau tanya")
    assert SESSION_STATE["d2"]["state"] == "MENU_CATEGORY"
    assert "KENDALA PESANAN" in r["options"]
    # Layer 1 (category menu): CC handoff is NOT offered here (only layer 3).
    assert HANDOFF_OPTION not in r["options"]


def test_answer_step_offers_ya_tidak_and_cc():
    SESSION_STATE.clear()
    _seed("d3")
    process_message("d3", "pesanan tidak masuk ke POS")
    choice = SESSION_STATE["d3"]["predefined_choices"][0]
    r = process_message("d3", choice)
    assert r["type"] == "question"
    assert r["options"] == ["Ya", "Tidak", HANDOFF_OPTION]
    assert SESSION_STATE["d3"]["state"] == "TROUBLESHOOTING"


def test_tidak_offers_cc_and_ticket():
    SESSION_STATE.clear()
    _seed("d4")
    process_message("d4", "pesanan tidak masuk ke POS")
    choice = SESSION_STATE["d4"]["predefined_choices"][0]
    process_message("d4", choice)
    r = process_message("d4", "Tidak")
    assert SESSION_STATE["d4"]["state"] == "CHOOSING_UNRESOLVED"
    assert HANDOFF_OPTION in r["options"]
    assert TICKET_OPTION in r["options"]


def test_unresolved_pick_ticket_starts_escalation():
    SESSION_STATE.clear()
    _seed("d5", state="CHOOSING_UNRESOLVED")
    r = process_message("d5", TICKET_OPTION)
    assert SESSION_STATE["d5"]["state"] == "COLLECTING_NAME"
    assert "nama" in r["text"].lower()


def test_unresolved_pick_cc_returns_handoff():
    SESSION_STATE.clear()
    _seed("d6", state="CHOOSING_UNRESOLVED")
    r = process_message("d6", HANDOFF_OPTION)
    assert r["type"] == "handoff_request"
    assert SESSION_STATE["d6"]["state"] == "HUMAN_HANDOFF"


def test_wrapup_off_topic_gets_confidential_decline():
    SESSION_STATE.clear()
    _seed("d7", state="WRAP_UP", last_ticket_number="Ticket #X")
    r = process_message("d7", "berapa gaji direktur ESB?")
    assert r["type"] == "message"
    assert r["text"] == CONFIDENTIAL_DECLINE


def test_wrapup_on_topic_is_acknowledged():
    SESSION_STATE.clear()
    _seed("d8", state="WRAP_UP", last_ticket_number="Ticket #Y")
    r = process_message("d8", "pesanan masih tidak masuk ke POS")
    assert r["type"] == "message"
    assert r["text"] != CONFIDENTIAL_DECLINE
    assert "Ticket #Y" in r["text"]
