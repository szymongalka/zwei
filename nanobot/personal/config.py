"""Explicit configuration for the optional personal platform."""

import re
from typing import Literal

from pydantic import ConfigDict, Field, SecretStr, field_validator, model_validator

from nanobot.config_base import Base


class PersonalConfig(Base):
    enabled: bool = False
    data_dir: str = "~/.nanobot/personal"
    postgres_file: str = ""
    embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    sync_interval_seconds: int = Field(default=300, ge=30, le=86400)
    sync_batch_size: int = Field(default=50, ge=1, le=500)
    evolution_enabled: bool = False
    evolution_interval_seconds: int = Field(default=21600, ge=300)
    evolution_min_samples: int = Field(default=12, ge=6)
    evolution_max_trials: int = Field(default=4, ge=1, le=12)
    retrieval_timeout_seconds: float = Field(default=2.0, ge=0.5, le=30)
    retrieval_dedup_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    development_enabled: bool = False
    development_interval_seconds: int = Field(default=86400, ge=3600)
    development_timeout_seconds: int = Field(default=1200, ge=60, le=3600)


class FolderRule(Base):
    """User-defined organization; no implicit taxonomy or destination folders."""
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    label: str = Field(min_length=1, max_length=100)
    folder: str = Field(min_length=1, max_length=200)
    contains: list[str] = Field(default_factory=list, max_length=50)
    senders: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("folder")
    @classmethod
    def valid_folder(cls, value: str) -> str:
        if any(ord(char) < 32 for char in value):
            raise ValueError("Folder names cannot contain control characters")
        return value


class Account(Base):
    """Server-owned account record. Secrets are never part of list responses."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    label: str = Field(min_length=1, max_length=100)
    kind: Literal["apple", "agent", "mailbox"]
    username: str = Field(min_length=1, max_length=254)
    password: SecretStr = SecretStr("")
    smtp_username: str = Field(default="", max_length=254)
    smtp_password: SecretStr = SecretStr("")
    enabled: bool = True
    include_inbox: bool = True
    organize_folders: bool = False
    folder_rules: list[FolderRule] = Field(default_factory=list, max_length=50)
    mail_enabled: bool = True
    calendar_enabled: bool = False
    contacts_enabled: bool = False
    send_enabled: bool | None = None
    imap_host: str = ""
    imap_port: int = Field(default=993, ge=1, le=65535)
    smtp_host: str = ""
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_security: Literal["starttls", "tls"] = "starttls"
    folders: list[str] = Field(default_factory=lambda: ["INBOX"], max_length=30)
    caldav_url: str = "https://caldav.icloud.com/"
    carddav_url: str = "https://contacts.icloud.com/"
    from_address: str = Field(default="", max_length=254)
    max_message_bytes: int = Field(default=25_000_000, ge=1024, le=100_000_000)

    @field_validator("imap_host", "smtp_host")
    @classmethod
    def hostname(cls, value: str) -> str:
        value = value.strip()
        if value and any(c in value for c in "/@\\\r\n\t "):
            raise ValueError("Use a hostname without a URL, credentials, or whitespace")
        return value

    @field_validator("username", "smtp_username", "from_address", "folders")
    @classmethod
    def no_controls(cls, value: str | list[str]) -> str | list[str]:
        values = value if isinstance(value, list) else [value]
        if any(any(ord(c) < 32 for c in v) for v in values):
            raise ValueError("Control characters are not allowed")
        if isinstance(value, list) and (not value or any(not v.strip() for v in value)):
            raise ValueError("Select at least one non-empty folder")
        return value

    @model_validator(mode="after")
    def defaults(self) -> "Account":
        if self.send_enabled is None:
            self.send_enabled = self.kind == "agent"
        if self.send_enabled and not self.from_address:
            self.from_address = self.username
        if self.kind == "apple":
            self.imap_host = self.imap_host or "imap.mail.me.com"
            self.smtp_host = self.smtp_host or "smtp.mail.me.com"
        if self.mail_enabled and not self.imap_host:
            raise ValueError("IMAP hostname is required")
        if self.organize_folders and not self.folder_rules:
            raise ValueError("Define organization rules before enabling folder moves")
        if self.send_enabled and (not self.smtp_host or not self.from_address):
            raise ValueError("Sending requires an SMTP hostname and a From address")
        if self.send_enabled and not re.fullmatch(r"[^@\s<>]+@[^@\s<>]+", self.from_address):
            raise ValueError("Use a plain email address for the sender")
        return self

    def public(self) -> dict[str, object]:
        result = self.model_dump(exclude={"password", "smtp_password"})
        result["has_password"] = bool(self.password.get_secret_value())
        result["has_smtp_password"] = bool(self.smtp_password.get_secret_value())
        return result
