from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
UPLOADS_DIR = DATA_DIR / "uploads"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    bot_token: str = ""
    admin_ids: str = ""
    admin_password: str = ""
    subgram_key: str = ""
    tgrass_key: str = ""
    botohub_key: str = ""
    session_secret: str = ""
    web_host: str = "0.0.0.0"
    web_port: int = 8000
    database_url: str = "postgresql+asyncpg://clickbot:change-me@postgres:5432/clickbot"
    proxy: str = ""
    proxy_scheme: str = "socks5"
    webapp_url: str = ""
    service_name: str = "autoposter"

    @property
    def admin_id_list(self) -> list[int]:
        ids = []
        for part in self.admin_ids.replace(" ", "").split(","):
            if part.strip().isdigit():
                ids.append(int(part.strip()))
        return ids

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_id_list

    @property
    def async_database_url(self) -> str:
        url = (self.database_url or "").strip()
        if not url:
            raise RuntimeError("DATABASE_URL не задан. Нужен PostgreSQL.")
        scheme, sep, rest = url.partition("://")
        if not sep:
            return url
        low = scheme.lower()
        if "sqlite" in low:
            raise RuntimeError("SQLite отключён. Поставь DATABASE_URL на PostgreSQL.")
        if low in ("postgres", "postgresql"):
            return f"postgresql+asyncpg://{rest}"
        if low == "postgresql+psycopg2":
            return f"postgresql+asyncpg://{rest}"
        return url

    @property
    def proxy_url(self) -> str | None:
        raw = (self.proxy or "").strip()
        if not raw:
            return None
        if "://" in raw:
            return raw
        parts = raw.split(":")
        scheme = (self.proxy_scheme or "http").strip() or "http"
        from urllib.parse import quote

        if len(parts) >= 4:
            host, port, user = parts[0], parts[1], parts[2]
            password = ":".join(parts[3:])
            return f"{scheme}://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}"
        if len(parts) == 2:
            return f"{scheme}://{parts[0]}:{parts[1]}"
        return raw


settings = Settings()

DEFAULT_SETTINGS: dict[str, str] = {
    "click_reward": "0.25",
    "click_cooldown": "0",
    "click_daily_limit": "0",
    "task_every_n": "5",
    "withdraw_min": "100",
    "ref_bonus": "5",
    "ref_threshold": "10",
    "ref_percent": "5",
    "withdraw_friends": "3",
    "withdraw_check_stars": "3",
    "star_fiat_rate": "1.8",
    "fiat_currency": "RUB",
    "bot_username": "",
    "welcome_text": "",
    "premium_emojis": "{}",
    "subgram_enabled": "1",
    "tgrass_enabled": "1",
    "botohub_enabled": "1",
    "subgram_key": "",
    "tgrass_key": "",
    "botohub_key": "",
    "last_daily_remind_date": "",
}
