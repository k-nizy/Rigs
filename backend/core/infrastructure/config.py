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

    # The largest single video object the service will take through
    # itself. Only the gateway-upload model reads a body into memory, and
    # this is the ceiling on what one request can cost. A presigned PUT
    # never touches this process and is not bounded here.
    max_video_bytes: int = 2 * 1024 * 1024 * 1024      # 2 GiB

    # A token per rig, placed on the machine by Ansible and sent on every
    # rig-facing call. The machine authenticates; the operator never does,
    # which is what keeps the rig a screen with no login.
    #
    # Read from the environment as JSON:
    #   RIG_TOKENS='{"RIG-01": "...", "RIG-02": "..."}'
    #
    # Empty means every rig is trusted, which is right for a laptop demo
    # and wrong for a floor. `auth_is_on` is what a deploy check reads,
    # and the service says so loudly at startup either way.
    rig_tokens: dict[str, str] = {}

    # The desk's write path. The plan decides per-rig auth at ingest and
    # deliberately does not decide this one, because the desk is expected
    # to sit behind the platform team's gateway, which already handles
    # cross-cutting concerns. This is here so a deployment that does NOT
    # have that in front of it can still close the hole: with it set, a
    # schedule push needs it. Unset, pushes are open and startup says so.
    desk_token: str = ""

    # SQLAlchemy pool. The default of 5 is sized for one process serving
    # requests; this service also runs three workers that each hold a
    # session while they work.
    db_pool_size: int = 10
    db_max_overflow: int = 10
    db_pool_recycle_secs: int = 1800   # under most idle-connection timeouts

    @property
    def auth_is_on(self) -> bool:
        return bool(self.rig_tokens)

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
