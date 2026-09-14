"""Settings — single source of truth for env-driven configuration.

Pydantic-settings loads .env (if present) and validates every value. Modules
should import `settings` and reach into it; do NOT call `os.environ.get(...)`
scattered across the codebase.

Tunable defaults match CLAUDE.md and adviserplan.md. Secrets have no defaults
so a missing value fails loudly at startup.
"""

from __future__ import annotations

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


_EMAIL_DEFAULTS: dict[str, dict[str, str | int]] = {
    "tencent": {
        "imap_host": "imap.exmail.qq.com",
        "smtp_host": "smtp.exmail.qq.com",
        "imap_port": 993,
        "smtp_port": 465,
    },
    "netease": {
        "imap_host": "imaphz.qiye.163.com",
        "smtp_host": "smtphz.qiye.163.com",
        "imap_port": 993,
        "smtp_port": 465,
    },
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- LLM ----
    openrouter_api_key: str = ""
    openrouter_model: str = "deepseek/deepseek-chat"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    # ---- Observability ----
    langsmith_api_key: str = ""
    langsmith_project: str = "hitl-support-agent"
    langsmith_tracing: bool = True
    # Empty → SDK default (US: https://api.smith.langchain.com).
    # Set to https://eu.api.smith.langchain.com for EU-region accounts.
    langsmith_endpoint: str = ""

    # ---- Enterprise email (Tencent by default; NetEase is one env switch) ----
    # The GMAIL_* aliases are intentionally kept for old deployments and
    # checkpoints. New installations should use EMAIL_* names.
    email_provider: str = Field(
        default="tencent",
        validation_alias=AliasChoices("EMAIL_PROVIDER", "MAIL_PROVIDER"),
    )
    email_user: str = Field(
        default="",
        validation_alias=AliasChoices("EMAIL_USER", "GMAIL_USER"),
    )
    email_app_password: str = Field(
        default="",
        validation_alias=AliasChoices("EMAIL_APP_PASSWORD", "GMAIL_APP_PASSWORD"),
    )
    email_imap_host: str = Field(
        default="",
        validation_alias=AliasChoices("EMAIL_IMAP_HOST"),
    )
    email_imap_port: int = Field(
        default=0,
        validation_alias=AliasChoices("EMAIL_IMAP_PORT"),
    )
    email_smtp_host: str = Field(
        default="",
        validation_alias=AliasChoices("EMAIL_SMTP_HOST"),
    )
    email_smtp_port: int = Field(
        default=0,
        validation_alias=AliasChoices("EMAIL_SMTP_PORT"),
    )
    # Empty means infer SSL for port 465 and STARTTLS for other ports.
    email_smtp_security: str = Field(
        default="",
        validation_alias=AliasChoices("EMAIL_SMTP_SECURITY"),
    )

    # Legacy values are separate so an explicit EMAIL_PROVIDER switch can
    # ignore stale Gmail endpoints left in an old .env file. They are copied
    # only when no new provider was selected, preserving old deployments.
    legacy_gmail_imap_host: str = Field(
        default="", validation_alias=AliasChoices("GMAIL_IMAP_HOST"), exclude=True
    )
    legacy_gmail_imap_port: int = Field(
        default=0, validation_alias=AliasChoices("GMAIL_IMAP_PORT"), exclude=True
    )
    legacy_gmail_smtp_host: str = Field(
        default="", validation_alias=AliasChoices("GMAIL_SMTP_HOST"), exclude=True
    )
    legacy_gmail_smtp_port: int = Field(
        default=0, validation_alias=AliasChoices("GMAIL_SMTP_PORT"), exclude=True
    )
    legacy_gmail_smtp_security: str = Field(
        default="", validation_alias=AliasChoices("GMAIL_SMTP_SECURITY"), exclude=True
    )

    # ---- Approval channel (Feishu by default; Slack remains a compatibility fallback) ----
    approval_provider: str = Field(
        default="feishu",
        validation_alias=AliasChoices("APPROVAL_PROVIDER"),
    )
    feishu_app_id: str = Field(default="", validation_alias=AliasChoices("FEISHU_APP_ID"))
    feishu_app_secret: str = Field(
        default="", validation_alias=AliasChoices("FEISHU_APP_SECRET")
    )
    feishu_receive_id_type: str = Field(
        default="chat_id",
        validation_alias=AliasChoices("FEISHU_RECEIVE_ID_TYPE"),
    )
    feishu_receive_id: str = Field(
        default="",
        validation_alias=AliasChoices("FEISHU_RECEIVE_ID", "FEISHU_CHAT_ID"),
    )
    feishu_verification_token: str = Field(
        default="",
        validation_alias=AliasChoices("FEISHU_VERIFICATION_TOKEN"),
    )
    feishu_encrypt_key: str = Field(
        default="",
        validation_alias=AliasChoices("FEISHU_ENCRYPT_KEY"),
    )
    feishu_api_base_url: str = Field(
        default="https://open.feishu.cn/open-apis",
        validation_alias=AliasChoices("FEISHU_API_BASE_URL"),
    )

    # Per-intent destinations. With Feishu, one test chat is enough; when the
    # per-intent values are empty, channel_set falls back to FEISHU_RECEIVE_ID.
    approval_channel_refunds: str = Field(
        default="",
        validation_alias=AliasChoices(
            "APPROVAL_CHANNEL_REFUNDS", "FEISHU_CHAT_REFUNDS", "SLACK_CHANNEL_REFUNDS"
        ),
    )
    approval_channel_technical: str = Field(
        default="",
        validation_alias=AliasChoices(
            "APPROVAL_CHANNEL_TECHNICAL",
            "FEISHU_CHAT_TECHNICAL",
            "SLACK_CHANNEL_TECHNICAL",
        ),
    )
    approval_channel_complaints: str = Field(
        default="",
        validation_alias=AliasChoices(
            "APPROVAL_CHANNEL_COMPLAINTS",
            "FEISHU_CHAT_COMPLAINTS",
            "SLACK_CHANNEL_COMPLAINTS",
        ),
    )

    # ---- Slack compatibility fallback ----
    slack_bot_token: str = ""
    slack_signing_secret: str = ""
    slack_app_token: str = ""

    # ---- Persistence ----
    sqlite_checkpoint_path: str = "./data/checkpoints.sqlite"

    # ---- PII vault ----
    # Empty → in-memory only (C1/C2 hardening: PII never on disk, never to
    # LangSmith). Non-empty → SQLite sidecar at the given path persists
    # envelope_from + token_map so a paused ticket can resume after process
    # death. Required for the "kill-mid-interrupt + resume" demo. Opt-in:
    # threat_model.md A1 documents the trade-off (recipient + redaction
    # token-map now sit on disk; mitigate with disk encryption / SELinux).
    pii_vault_db_path: str = ""

    # ---- Tunables (defaults match CLAUDE.md) ----
    revalidate_threshold_min: int = 15
    max_human_rejections: int = 3
    max_send_retries: int = 3
    sla_deadline_hours: int = 24
    imap_poll_interval_sec: int = 30

    # ---- Server ----
    # Default to loopback only (127.0.0.1) — safer default for dev. Deployment
    # configs that need to accept external traffic must explicitly set HOST=0.0.0.0
    # via the env (Docker, container platforms typically already do this).
    # bandit B104 was triggered by the previous "0.0.0.0" default; threat model
    # documents the choice in docs/threat_model.md.
    host: str = "127.0.0.1"
    port: int = 8000

    @property
    def channel_set(self) -> dict[str, str]:
        """Map intent keys to Feishu receive IDs or legacy Slack channels."""
        defaults = {
            "refunds": "#support-refunds",
            "technical": "#support-technical",
            "complaints": "#support-complaints",
        }
        configured = {
            "refunds": self.approval_channel_refunds,
            "technical": self.approval_channel_technical,
            "complaints": self.approval_channel_complaints,
        }
        fallback = self.feishu_receive_id if self.approval_provider.lower() == "feishu" else ""
        return {
            key: value or fallback or defaults[key] for key, value in configured.items()
        }

    # Read-only aliases for older code and previously documented settings.
    @property
    def gmail_user(self) -> str:
        return self.email_user

    @property
    def gmail_app_password(self) -> str:
        return self.email_app_password

    @property
    def gmail_imap_host(self) -> str:
        return self.email_imap_host

    @property
    def gmail_imap_port(self) -> int:
        return self.email_imap_port

    @property
    def gmail_smtp_host(self) -> str:
        return self.email_smtp_host

    @property
    def gmail_smtp_port(self) -> int:
        return self.email_smtp_port

    @property
    def slack_channel_refunds(self) -> str:
        return self.channel_set["refunds"]

    @property
    def slack_channel_technical(self) -> str:
        return self.channel_set["technical"]

    @property
    def slack_channel_complaints(self) -> str:
        return self.channel_set["complaints"]

    @model_validator(mode="after")
    def apply_email_provider_defaults(self) -> "Settings":
        """Select provider endpoints while preserving explicit env overrides."""
        provider = self.email_provider.strip().lower()
        if provider not in _EMAIL_DEFAULTS:
            allowed = ", ".join(sorted(_EMAIL_DEFAULTS))
            raise ValueError(f"EMAIL_PROVIDER must be one of: {allowed}")

        # Capture this before normalising the provider below; assignment adds
        # the field to model_fields_set in Pydantic v2.
        use_legacy = "email_provider" not in self.model_fields_set
        self.email_provider = provider
        defaults = _EMAIL_DEFAULTS[provider]
        if not self.email_imap_host:
            if use_legacy and self.legacy_gmail_imap_host:
                self.email_imap_host = self.legacy_gmail_imap_host
            else:
                self.email_imap_host = str(defaults["imap_host"])
        if not self.email_smtp_host:
            if use_legacy and self.legacy_gmail_smtp_host:
                self.email_smtp_host = self.legacy_gmail_smtp_host
            else:
                self.email_smtp_host = str(defaults["smtp_host"])
        if self.email_imap_port <= 0:
            if use_legacy and self.legacy_gmail_imap_port > 0:
                self.email_imap_port = self.legacy_gmail_imap_port
            else:
                self.email_imap_port = int(defaults["imap_port"])
        if self.email_smtp_port <= 0:
            if use_legacy and self.legacy_gmail_smtp_port > 0:
                self.email_smtp_port = self.legacy_gmail_smtp_port
            else:
                self.email_smtp_port = int(defaults["smtp_port"])
        if not self.email_smtp_security:
            if use_legacy and self.legacy_gmail_smtp_security:
                self.email_smtp_security = self.legacy_gmail_smtp_security
            else:
                self.email_smtp_security = "ssl" if self.email_smtp_port == 465 else "starttls"
        return self

    def require_secrets(self, *names: str) -> None:
        """Fail loudly if a secret is empty. Call at startup of each I/O module."""
        missing = [n for n in names if not getattr(self, n, "")]
        if missing:
            raise RuntimeError(
                f"Missing required secrets in .env: {', '.join(missing)}. "
                f"See .env.example for the full list."
            )


settings = Settings()
