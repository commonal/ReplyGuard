"""Reply-language eval set — does the drafter mirror the customer's language?

Why this set exists
-------------------
Every other dataset in this harness is English-only (curated, Bitext,
adversarial). The drafter had no language rule at all until now, and the prompts
plus the whole `acme_policies.md` corpus are English, so nothing here could catch
a Chinese ticket being answered in English — that failure would have scored
perfectly on every existing metric.

Run it live (`--dataset language`). A `--no-llm` run only exercises the harness
plumbing: the canned drafts are what they are, so the canned Chinese tickets
"pass" trivially. The behaviour under test is the model's.

The cases, in order (id — inbound — expected reply language):

  lang-t01  Chinese, long               zh
  lang-t02  Chinese, info intent        zh
  lang-t03  Mixed Chinese + English     zh    mixed input is Chinese by contract
  lang-t04  Short Chinese (退款)         zh    was answered in English before the rule
  lang-t05  Chinese with an order no.   zh
  lang-t06  Digits and symbols only     zh    the only genuine fallback case
  lang-t07  English, long               en
  lang-t08  Short English (refund)      en    an early, looser rule wrongly sent this to zh

Why `expected_outcome` is "escalated" throughout
------------------------------------------------
This set measures ONE thing: the reply language. It deliberately asserts nothing
about routing, and the first version of the file got that wrong by declaring
`auto_send` for every ticket — then the live run escalated all eight.

The reason is real and worth recording: on the v4 path the drafter's own
`draft_confidence` lands around 0.6 for these messages while the classifier is
confident (`intent_confidence` 0.95, no risk flags), so **Gate 2 fires on the
draft score alone** and every one of them goes to a human. That is the
conservative behaviour this project wants, not a defect, and it is why the
canned `draft_confidence` of 0.95 here is a fiction that only exists so a
`--no-llm` run can reach the auto-send branch.

Routing is measured where it belongs: eval/dataset.py (one ticket per code path)
and the Bitext sets. Do not read a routing conclusion out of this file.

All intents are in AUTO_SEND_SAFE_INTENTS, so when the draft score does clear
the threshold the run needs no approval channel and no SMTP.
"""

from __future__ import annotations

from eval.dataset import EvalTicket

_ZH_DRAFT = "您好，感谢咨询。您可以在「设置」页面完成相关操作，如需帮助请告知。"


def _ticket(
    ticket_id: str,
    inbound: str,
    expected_language: str,
    description: str,
    draft: str,
    intent: str = "FAQ",
) -> EvalTicket:
    """One language-case ticket.

    `expected_outcome` is "escalated" for every ticket: see the module docstring
    — on the v4 path the draft score does not clear Gate 2 for these messages,
    and this set does not assert routing anyway. The channel is the catch-all
    the router picks for these intents.
    """
    return EvalTicket(
        ticket_id=ticket_id,
        description=f"{description} -> reply in {expected_language}",
        customer_message=inbound,
        customer_email="customer@example.com",
        expected_intent=intent,
        expected_outcome="escalated",
        expected_channel="#support-technical",
        expected_risk_flags=[],
        canned_classification={
            "intent": intent,
            "intent_confidence": 0.95,
            "sentiment": "neutral",
            "risk_flags": [],
            "risk_level": "none",
        },
        canned_draft={"draft": draft, "draft_confidence": 0.95},
        expected_language=expected_language,
    )


LANGUAGE_TICKETS: list[EvalTicket] = [
    _ticket(
        "lang-t01",
        "From: customer@example.com\nSubject: 如何重置密码\n\n"
        "你好，我忘记密码了，请问怎么重置？",
        "zh",
        "Chinese, long",
        "您好，请在登录页点击「忘记密码」，重置邮件会在两分钟内发送到您的邮箱。",
    ),
    _ticket(
        "lang-t02",
        "From: customer@example.com\nSubject: 咨询\n\n"
        "你好，我想咨询一下账户设置的问题，麻烦帮我看一下，谢谢。",
        "zh",
        "Chinese, info intent",
        _ZH_DRAFT,
        intent="info",
    ),
    _ticket(
        "lang-t03",
        "From: customer@example.com\nSubject: 混排\n\n"
        "Hello 你好, I want to know 如何 reset 我的 password please 谢谢。",
        "zh",
        "Mixed Chinese + English",
        "您好，重置密码的方法：请点击登录页的「忘记密码」链接。",
    ),
    _ticket(
        "lang-t04",
        "From: customer@example.com\nSubject: 退款\n\n退款",
        "zh",
        "Short Chinese",
        "您好，关于退款，请您提供订单号，我们会尽快为您处理。",
    ),
    _ticket(
        "lang-t05",
        "From: customer@example.com\nSubject: 发票\n\n"
        "请问发票在哪里下载？订单号 12345。",
        "zh",
        "Chinese with an order number",
        "您好，发票可在「账单」页面下载，感谢您提供订单号 12345。",
        intent="info",
    ),
    _ticket(
        "lang-t06",
        "From: customer@example.com\nSubject: ???\n\n???!!!",
        "zh",
        "Digits and symbols only (genuine fallback)",
        "您好，没有收到具体问题描述，请补充说明，我们会尽快为您处理。",
    ),
    _ticket(
        "lang-t07",
        "From: customer@example.com\nSubject: How do I reset my password?\n\n"
        "Hi, I forgot my password. How do I reset it?",
        "en",
        "English, long",
        "Hi there, go to the login page and click 'Forgot Password'. "
        "The reset email arrives within two minutes.",
    ),
    _ticket(
        "lang-t08",
        "From: customer@example.com\nSubject: Refund\n\nrefund",
        "en",
        "Short English",
        "Hi there, to process a refund please share your order number and we "
        "will take care of it.",
    ),
]

__all__ = ["LANGUAGE_TICKETS"]
