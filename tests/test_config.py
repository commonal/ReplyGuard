"""Configuration tests for domestic enterprise-mail and Feishu defaults."""

from __future__ import annotations

from src.config import Settings


def test_tencent_is_the_default_email_provider() -> None:
    settings = Settings(_env_file=None)

    assert settings.email_provider == "tencent"
    assert settings.email_imap_host == "imap.exmail.qq.com"
    assert settings.email_smtp_host == "smtp.exmail.qq.com"
    assert settings.email_smtp_security == "ssl"


def test_netease_provider_selects_netease_endpoints() -> None:
    settings = Settings(_env_file=None, EMAIL_PROVIDER="netease")

    assert settings.email_provider == "netease"
    assert settings.email_imap_host == "imaphz.qiye.163.com"
    assert settings.email_smtp_host == "smtphz.qiye.163.com"
    assert settings.email_smtp_port == 465


def test_explicit_email_endpoints_override_provider_defaults() -> None:
    settings = Settings(
        _env_file=None,
        EMAIL_PROVIDER="netease",
        EMAIL_IMAP_HOST="imap.custom.example",
        EMAIL_SMTP_PORT=587,
        EMAIL_SMTP_SECURITY="starttls",
    )

    assert settings.email_imap_host == "imap.custom.example"
    assert settings.email_smtp_host == "smtphz.qiye.163.com"
    assert settings.email_smtp_port == 587
    assert settings.email_smtp_security == "starttls"
