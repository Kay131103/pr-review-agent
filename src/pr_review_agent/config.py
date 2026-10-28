"""Runtime configuration, loaded from environment variables (prefix ``PR_REVIEW_``) or ``.env``."""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"

    @property
    def rank(self) -> int:
        return {"info": 0, "warning": 1, "error": 2}[self.value]


def _split_csv(value: object) -> object:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PR_REVIEW_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- GitHub authentication: either a token, or GitHub App credentials ---
    github_token: SecretStr | None = None
    github_app_id: str | None = None
    github_app_private_key: SecretStr | None = None
    github_app_private_key_path: Path | None = None
    github_api_url: str = "https://api.github.com"
    webhook_secret: SecretStr | None = None

    # --- Claude review (optional; rule-based checks always run) ---
    anthropic_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "anthropic_api_key", "PR_REVIEW_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"
        ),
    )
    llm_enabled: bool = True
    llm_model: str = "claude-opus-5-5"
    llm_effort: str = "medium"
    llm_max_tokens: int = 16000
    llm_timeout_seconds: float = 600.0

    # --- Review behaviour ---
    review_drafts: bool = False
    max_files: int = 100
    max_patch_chars: int = 300_000
    max_inline_comments: int = 30
    fail_on: Severity = Severity.ERROR
    request_changes_on_fail: bool = False
    status_context: str = "pr-review-agent"
    ignore_paths: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "*.lock",
            "package-lock.json",
            "pnpm-lock.yaml",
            "*.min.js",
            "*.min.css",
            "*.svg",
            "vendor/*",
            "dist/*",
        ]
    )
    disabled_rules: Annotated[list[str], NoDecode] = Field(default_factory=list)
    review_command: str = "/review"

    # --- Watcher (polling) ---
    watch_repos: Annotated[list[str], NoDecode] = Field(default_factory=list)
    poll_interval_seconds: int = 120

    # --- Runtime ---
    workers: int = 4
    queue_size: int = 200
    db_path: Path = Path("pr_review_agent.db")
    log_level: str = "INFO"
    log_json: bool = False
    host: str = "0.0.0.0"
    port: int = 8080

    _split = field_validator("ignore_paths", "disabled_rules", "watch_repos", mode="before")(
        _split_csv
    )

    @field_validator("watch_repos")
    @classmethod
    def _validate_repos(cls, repos: list[str]) -> list[str]:
        for repo in repos:
            if repo.count("/") != 1 or not all(repo.split("/")):
                raise ValueError(f"watch_repos entry must look like 'owner/name', got {repo!r}")
        return repos

    @model_validator(mode="after")
    def _validate_auth(self) -> Settings:
        if self.github_app_id and not (
            self.github_app_private_key or self.github_app_private_key_path
        ):
            raise ValueError("github_app_id requires github_app_private_key(_path)")
        return self

    @property
    def uses_github_app(self) -> bool:
        return bool(self.github_app_id)

    @property
    def llm_active(self) -> bool:
        return self.llm_enabled and self.anthropic_api_key is not None

    def app_private_key(self) -> str:
        if self.github_app_private_key:
            return self.github_app_private_key.get_secret_value().replace("\\n", "\n")
        if self.github_app_private_key_path:
            return self.github_app_private_key_path.read_text(encoding="utf-8")
        raise ValueError("GitHub App private key is not configured")


@lru_cache
def get_settings() -> Settings:
    return Settings()
