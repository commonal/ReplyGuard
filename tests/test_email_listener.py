"""Focused tests for the IMAP listener's safe test filter."""

from __future__ import annotations

from email.header import Header

from src import email_listener


def test_unseen_search_is_unfiltered_by_default(monkeypatch) -> None:
    monkeypatch.setattr(email_listener.settings, "imap_subject_filter", "")
    monkeypatch.setattr(email_listener.settings, "imap_max_messages", 0)
    assert email_listener._unseen_search_criteria() == ("UNSEEN",)


def test_unseen_search_can_be_narrowed_to_subject(monkeypatch) -> None:
    monkeypatch.setattr(email_listener.settings, "imap_subject_filter", 'ReplyGuard-"test"')
    assert email_listener._unseen_search_criteria() == (
        "UNSEEN",
        "SUBJECT",
        '"ReplyGuard-\\"test\\""',
    )


def test_decode_header_accepts_email_header_objects() -> None:
    assert email_listener._decode_header(Header("ReplyGuard-Test-20260915-A")) == (
        "ReplyGuard-Test-20260915-A"
    )
