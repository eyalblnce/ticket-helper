from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).parent.parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    # Freshdesk
    freshdesk_api_key: str
    freshdesk_domain: str  # e.g. "yourcompany.freshdesk.com"

    # Freshchat (optional until credentials are available)
    freshchat_token: str = ""
    freshchat_domain: str = ""

    # Anthropic
    anthropic_api_key: str = ""

    # App
    basic_auth_user: str = "admin"
    basic_auth_password: str = "changeme"
    database_url: str = "sqlite:///./dev.db"

    # Training pipeline
    training_budget_usd: float = 5.0
    training_min_cluster_size: int = 15
    training_min_cluster_size_for_sop: int = 15
    training_max_tickets_per_cluster: int = 20
    training_holdout_fraction: float = 0.2
    training_synthesis_concurrency: int = 3


settings = Settings()
