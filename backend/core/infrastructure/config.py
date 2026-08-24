"""Settings, read once from the environment.

`backend/.env` carries the database password and is gitignored. Nothing
in this package ever logs a connection URL - `safe_url` exists so a
startup line can say which database it reached without saying how.
"""

from functools import lru_cache
from urllib.parse import urlsplit, urlunsplit

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    database_url: str
    test_database_url: str = ""

    # How long a rig may be silent before the floor sweep calls it missing.
    # An absence has no event to subscribe to, so it has to be looked for.
    rig_silent_after_secs: int = 180

    # Where video goes. With no endpoint configured the local stand-in is
    # used, which is a directory - real enough to exercise the whole path
    # and honest about not being S3.
    storage_endpoint: str = ""
    storage_bucket: str = "rigs-video"
    storage_access_key: str = ""
    storage_secret_key: str = ""
    storage_local_root: str = "./.storage"

    def safe_url(self, url: str | None = None) -> str:
        """The URL with the password removed, for logs and health output."""
        parts = urlsplit(url or self.database_url)
        if parts.password:
            host = parts.hostname or ""
            if parts.port:
                host = f"{host}:{parts.port}"
            netloc = f"{parts.username}:***@{host}"
            parts = parts._replace(netloc=netloc)
        return urlunsplit(parts)


@lru_cache
def get_settings() -> Settings:
    return Settings()
