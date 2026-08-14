from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FSTEC_", env_file=".env", extra="ignore")
    database_url: str = "sqlite:///./data/fstec-monitor.db"
    storage_dir: Path = Path("./data/objects")
    base_url: str = "https://fstec.ru"
    catalog_url: str = "https://fstec.ru/dokumenty/vse-dokumenty"
    user_agent: str = "FSTEC-Monitor/0.1"
    request_delay_seconds: float = 2.0
    timeout_seconds: float = 45.0
    max_retries: int = 3
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None

settings = Settings()
