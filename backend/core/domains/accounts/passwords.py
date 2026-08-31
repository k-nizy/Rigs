"""How a person's password is stored, and the one place that changes.

Two hashes live in this system and they are deliberately different, which
is the thing to understand before changing either.

**A password is slow on purpose.** People choose low-entropy secrets, so
the only defence against a stolen table is making each guess expensive.
That is what scrypt is for, and why a login costs a visible fraction of a
second.

**A session token is fast on purpose.** It is 256 bits from `secrets`,
so there is nothing to guess and no work factor would add anything. It is
hashed with SHA-256 - once per request, not once per shift - because
putting scrypt on the request path would buy no security and cost every
authenticated call half a second. See `token_fingerprint` below.

Stdlib, so this adds no dependency to a service that has been careful
about them. argon2id is the modern first choice and this module is the
seam for it: `hash_password` and `verify_password` are the only two
functions anything else calls, and the stored string names its own
algorithm, so a swap does not invalidate a single existing hash.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

# OWASP lists n=2**15, r=8, p=3 as one of the accepted scrypt settings.
# It is chosen here over the n=2**17, p=1 variant for the same total work
# at a quarter of the memory: this process also runs alongside three
# workers, and 128 MiB per concurrent login is a worse failure than a
# slightly longer one.
SCRYPT_N = 2 ** 15
SCRYPT_R = 8
SCRYPT_P = 3
SALT_BYTES = 16
KEY_BYTES = 32

# What a password has to be, in one place because there are now two ways
# to set one - `tools.mint_account` and `POST /api/auth/password` - and a
# rule written twice is a rule that will hold in one of them.
#
# It used to live in mint_account's interactive prompt alone, which meant
# `--password` set anything at all: the check was on the way the password
# was *typed* rather than on the password. Length only, deliberately. A
# composition rule ("one capital, one digit") pushes people towards
# `Password1!` and NIST stopped recommending it; length is the part that
# actually costs a guesser something.
MIN_PASSWORD_LENGTH = 12


def password_complaint(password: str) -> str | None:
    """What is wrong with this as a password, or None if nothing is.

    A sentence rather than a boolean, because both callers have to tell
    somebody what to do about it, and two independently worded versions
    of the same rule is how they drift.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        return (f"too short - {MIN_PASSWORD_LENGTH} characters at the very "
                f"least, and a phrase beats a short scramble")
    return None


def _maxmem(n: int, r: int, p: int) -> int:
    """The memory ceiling to hand OpenSSL, derived rather than guessed.

    scrypt needs 128*N*r bytes. OpenSSL's default ceiling is 32 MiB,
    which n=2**15, r=8 hits *exactly* - so the obvious parameters raise
    `memory limit exceeded` on the very first call rather than at some
    load. Passing a ceiling computed from the parameters in use means
    raising the cost later cannot reintroduce that.
    """
    return 128 * n * r * p + (1 << 20)


def hash_password(password: str) -> str:
    """A self-describing hash: `scrypt$n=...,r=...,p=...$salt$key`.

    The parameters travel with the hash so the cost can be raised without
    invalidating a single stored password - `verify_password` reads what
    that hash was actually made with, not what the constants say today.
    """
    salt = secrets.token_bytes(SALT_BYTES)
    key = hashlib.scrypt(
        password.encode("utf-8"), salt=salt,
        n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=KEY_BYTES,
        maxmem=_maxmem(SCRYPT_N, SCRYPT_R, SCRYPT_P),
    )
    return "scrypt$n={},r={},p={}${}${}".format(
        SCRYPT_N, SCRYPT_R, SCRYPT_P, _b64(salt), _b64(key)
    )


def verify_password(password: str, stored: str) -> bool:
    """True if this password made that hash. Never raises.

    A malformed or unknown-algorithm hash is False, not an exception: the
    login route must answer the same way for a broken row as for a wrong
    password, and an unhandled 500 tells a prober that the account exists.
    """
    try:
        algo, params, salt_b64, key_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        n, r, p = _params(params)
        want = _unb64(key_b64)
        got = hashlib.scrypt(
            password.encode("utf-8"), salt=_unb64(salt_b64),
            n=n, r=r, p=p, dklen=len(want), maxmem=_maxmem(n, r, p),
        )
    except (ValueError, KeyError, TypeError):
        return False
    # Not ==. An early return on the first wrong byte is the same leak the
    # rig's token check avoids, for the same reason.
    return hmac.compare_digest(got, want)


def needs_rehash(stored: str) -> bool:
    """Whether this hash was made at a weaker setting than today's.

    The login route is the only place that knows the plaintext, so it is
    the only place that can upgrade one. Without this, raising the cost
    protects new accounts and leaves every existing one where it was.
    """
    try:
        algo, params, _, _ = stored.split("$")
        if algo != "scrypt":
            return True
        n, r, p = _params(params)
    except (ValueError, KeyError):
        return True
    return (n, r, p) != (SCRYPT_N, SCRYPT_R, SCRYPT_P)


def token_fingerprint(token: str) -> str:
    """What goes in the database in place of a session token.

    A dump of `account_sessions` must not be a set of live sessions - the
    same instinct as never logging a token. SHA-256 and not scrypt: see
    the module docstring. This runs on every authenticated request.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _params(text: str) -> tuple[int, int, int]:
    kv = dict(part.split("=") for part in text.split(","))
    return int(kv["n"]), int(kv["r"]), int(kv["p"])


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))
