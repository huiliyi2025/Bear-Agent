"""Runtime configuration.

Values come from (highest precedence first):
1. explicit CLI flags (applied in main.py),
2. ``BEAR_*`` environment variables,
3. a ``.env`` file in the working directory,
4. built-in defaults.
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BEAR_",
        env_file=".env",
        extra="ignore",
    )

    model: str = "gpt-4o-mini"
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    request_timeout: float = 120.0
    temperature: float = 0.0
    max_tokens: int | None = 4096
    max_steps: int = 12
    working_dir: Path = Path.cwd()
    plan_mode: bool = False

    @property
    def is_local_provider(self) -> bool:
        host = self.base_url.lower()
        return any(token in host for token in ("localhost", "127.0.0.1", "ollama"))
