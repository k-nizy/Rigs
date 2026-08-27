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

    # How far a rig's clock may disagree with the server before it is
    # worth saying so. The measurement includes one-way latency, so this
    # is deliberately not tight - it is here to catch a machine that came
    # up without NTP, not to police milliseconds.
    clock_skew_tolerance_secs: int = 120

    # How long an event may wait to become a fact before the service says
    # it has fallen behind. A dead projection worker publishes nothing, so
    # this is the only thing that notices.
    projection_behind_after_secs: int = 120

    # Where video goes. With no endpoint configured the local stand-in is
    # used, which is a directory - real enough to exercise the whole path
    # and honest about not being S3.
    storage_endpoint: str = ""
    storage_bucket: str = "rigs-video"
    storage_access_key: str = ""
    storage_secret_key: str = ""
    storage_local_root: str = "./.storage"

    # How many days archived video is kept. Decided: 90.
    #
    # The default lives here rather than only in .env because .env does
    # not travel - when core/ and services/rigs/ are lifted into the
    # platform team's tree, this file goes and that one does not. A policy
    # that only exists in an environment variable is a policy that gets
    # lost at the handover and silently becomes "keep everything".
    #
    # At the plan's own sizing - three 1080p30 cameras at ~7 Mbps across
    # twelve rigs, ~2.7 TB a day - ninety days is a steady state of
    # roughly 245 TB, reached after ninety days and flat from then on.
    # That is the number the cold tier has to be provisioned for.
    #
    # 0 still means keep everything for ever, and is what to set if the
    # answer ever changes back.
    video_keep_days: int = 90

    # How long a camera may sit "pending" - the rig asked where to put a
    # take and never delivered it - before the service stops waiting.
    # Decided: 7 days. A weekend plus a rig away for repair.
    #
    # Nothing is deleted; the bytes were never here. It only stops a take
    # nobody will ever send from sitting in the backlog that is supposed
    # to say whether the spool is healthy. A late arrival heals itself,
    # because confirm() does not care what the row said before.
    video_pending_after_days: int = 7

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

    # Which address each rig calls from, so the service can tell the twelve
    # machines apart before any of them has said who it is.
    #
    #   RIG_ADDRESSES='{"RIG-01": "10.0.0.11", "RIG-02": "10.0.0.12"}'
    #
    # The token above answers "is this caller allowed to be RIG-07". This
    # answers the question that comes first and used to have no answer at
    # all: which rig is this machine? The kiosk loads its page from the
    # server, so a per-machine file placed beside the app on the rig is
    # never read - the browser fetches the server's copy. Identity has to
    # come from something the server can observe about the caller, and on
    # a floor where the rigs and the service share a switch that is the
    # address.
    #
    # What it is worth: a machine that is not at RIG-07's address cannot
    # obtain RIG-07's token, so a laptop plugged into the floor switch
    # gets nothing. What it is not worth: anything against somebody who
    # can already take that address. It is a provisioning mechanism, not
    # a defence against an attacker on the floor network - the token
    # behind it is what authenticates, and this only decides who is
    # handed one.
    #
    # Empty means nobody is identified, which is right for a laptop demo
    # and wrong for a floor. Preflight compares it against RIG_TOKENS,
    # because a rig with a token and no address can never fetch it, and a
    # rig with an address and no token is handed one that will be refused.
    rig_addresses: dict[str, str] = {}

    # The desk's write path. The plan decides per-rig auth at ingest and
    # deliberately does not decide this one, because the desk is expected
    # to sit behind the platform team's gateway, which already handles
    # cross-cutting concerns. This is here so a deployment that does NOT
    # have that in front of it can still close the hole: with it set, a
    # schedule push needs it. Unset, pushes are open and startup says so.
    desk_token: str = ""

    # Requests per minute per rig on the rig-facing routes. 0 is off,
    # and off is the default because the platform team's gateway may
    # already own this - two limiters disagreeing is worse than one.
    #
    # A ceiling, not a target: the floor's sustained rate is about 0.01
    # requests per second per rig. What this is sized for is a rig coming
    # back from an outage and emptying its outbox, which is correct
    # behaviour and must not be punished. Being refused is safe - the rig
    # keeps the events, backs off, and ingest dedupes the retry.
    rig_rate_limit_per_min: int = 0

    # ------------------------------------------------------------ people
    #
    # The desk is a screen a person uses, and until now nothing knew which
    # person. These settle how long they stay signed in and how the cookie
    # that says so is protected.

    # A shift is eight hours. Twelve covers one plus the handover either
    # side, so signing in at the top of a shift cannot throw somebody out
    # in the middle of it.
    session_lifetime_hours: int = 12

    # Whether the session cookie is marked Secure - HTTPS only.
    #
    # True by default, and deliberately the opposite way round from every
    # other switch in this file. The others are off until configured
    # because a laptop demo should not need setup; this one protects a
    # credential, and a security control whose default is the unsafe
    # setting is one that ships unsafe. Local development over plain http
    # sets SESSION_COOKIE_SECURE=false and startup says so out loud.
    session_cookie_secure: bool = True

    # Attempts per minute per calling address on the login route.
    #
    # On by default, unlike rig_rate_limit_per_min, and the difference is
    # not an inconsistency. That one is off because the platform team's
    # gateway may already own it and two limiters disagreeing is worse
    # than one. This guards a password: unthrottled, it is a brute-force
    # target whoever is in front of it. Being wrong in that direction
    # costs a floor its schedule; being wrong in this one costs a manager
    # a sixty-second wait, once.
    login_rate_limit_per_min: int = 10

    # Whether reading the floor needs the desk token as well as writing to
    # it. Off by default: /floor/state is what a wall display shows and
    # what the desk polls, and making it need a secret is a product
    # decision rather than a security default. Writes are gated by
    # desk_token on their own.
    protect_floor_reads: bool = False

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
