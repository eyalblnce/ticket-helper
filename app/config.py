from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

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


settings = Settings()
