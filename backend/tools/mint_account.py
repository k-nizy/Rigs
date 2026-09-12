"""Create and manage the people who can sign in.

There is no sign-up page and there should not be. A floor has two or
three managers and sixteen operators on a roster somebody already
maintains; a self-service registration form on the screen that pushes
schedules to twelve rigs is a door where a wall belongs.

    python -m tools.mint_account list
    python -m tools.mint_account manager  --email r.osei@verlet.co --name "Ruth Osei"
    python -m tools.mint_account operator --email m.chen@verlet.co --name "Mei Chen" --person <people.id>
    python -m tools.mint_account link     --email m.chen@verlet.co --person <people.id>   # an account that predates people
    python -m tools.mint_account invite   --person <people.id>   # an account with no password, and a link to set one
    python -m tools.mint_account passwd   --email r.osei@verlet.co
    python -m tools.mint_account disable  --email r.osei@verlet.co

This is also how the *first* manager exists at all. Every alternative to
somebody running this once is worse: a seeded default account is a known
password on every deployment, and a first-run setup page is an open door
for however long it takes the first person to find it.

With no password given, one is generated and printed. That is the better
default - it is strong, it is used once, and the person changes it. It is
printed to a terminal, which is not a safe place to leave it: hand it
over and clear the scrollback.

Disabling, not deleting. It ends every session the person has open, and
keeps the name so a later audit row still resolves to somebody.
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
import sys
import uuid
from datetime import datetime, timezone
from getpass import getpass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select                                    # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from core.domains.accounts.model import Account, MANAGER, OPERATOR  # noqa: E402
from core.domains.accounts.passwords import (                    # noqa: E402
    hash_password, password_complaint,
)
from services.rigs.people import InviteRefused, invite_person   # noqa: E402
from core.domains.people.repository import PersonRepository
from core.domains.accounts.repository import (                   # noqa: E402
    AccountRepository, AccountSessionRepository, normalise_email,
)
from core.infrastructure.config import get_settings              # noqa: E402


def _password(given: str | None, prompt: bool) -> tuple[str, bool]:
    """The password to set, and whether it has to be shown afterwards.

    The rule is checked on the password, not on the way it was typed.
    `--password` used to skip it entirely - the length test sat inside
    the interactive branch - so the one path a script would use was the
    one path with no rule at all.
    """
    if given:
        complaint = password_complaint(given)
        if complaint:
            raise SystemExit(complaint)
        return given, False
    if prompt:
        first = getpass("password: ")
        if first != getpass("again: "):
            raise SystemExit("passwords did not match")
        complaint = password_complaint(first)
        if complaint:
            raise SystemExit(complaint)
        return first, False
    # Generated, so it is long and random by construction rather than by
    # check - but asserted anyway, so raising the minimum past what this
    # produces fails here instead of minting accounts that breach it.
    made = secrets.token_urlsafe(12)
    assert password_complaint(made) is None, "the generated password breaks the rule"
    return made, True


async def _run(args) -> int:
    engine = create_async_engine(get_settings().database_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as session:
            return await _act(args, session)
    finally:
        await engine.dispose()


async def _act(args, session) -> int:
    accounts = AccountRepository(session)

    if args.cmd == "list":
        rows = (await session.execute(
            select(Account).order_by(Account.role, Account.email))).scalars().all()
        if not rows:
            print("No accounts. The service treats that as person auth being off.")
            return 0
        for a in rows:
            state = "disabled" if a.disabled_at else "active"
            print(f"{a.role:<9} {a.email:<32} {a.name:<22} "
                  f"{str(a.person_id)[:8] if a.person_id else '-':<8} {state}")
        return 0

    if args.cmd == "invite":
        # The desk's invite button, from here: the same function, so the
        # two cannot drift. Needs the floor's mail relay configured; on a
        # floor without one, `operator --person` with a password is the
        # way, and the refusal says so.
        try:
            person_id = uuid.UUID(str(args.person))
        except (ValueError, TypeError):
            raise SystemExit(f"--person must be a people id, got {args.person!r}")
        try:
            account, what, sent = await invite_person(session, person_id, get_settings())
        except LookupError:
            raise SystemExit(f"nobody has the id {person_id} - create the person on the desk first")
        except InviteRefused as refused:
            raise SystemExit(refused.message)
        print(f"{'created' if what == 'created' else 'found'} {account.email}; "
              + ("invitation sent" if sent else "the relay refused the mail - see the log"))
        return 0 if sent else 1

    existing = await accounts.by_email(args.email)

    if args.cmd == "disable":
        if not existing:
            raise SystemExit(f"no account for {args.email}")
        existing.disabled_at = datetime.now(timezone.utc)
        ended = await AccountSessionRepository(session).revoke_all(existing.id)
        await session.commit()
        print(f"disabled {existing.email}; ended {ended} open session(s)")
        return 0

    if args.cmd == "passwd":
        if not existing:
            raise SystemExit(f"no account for {args.email}")
        password, show = _password(args.password, prompt=not args.generate)
        existing.password_hash = hash_password(password)
        existing.password_set_at = datetime.now(timezone.utc)
        # A changed password that leaves the old cookies working has not
        # changed anything for whoever already had one.
        ended = await AccountSessionRepository(session).revoke_all(existing.id)
        await session.commit()
        print(f"password set for {existing.email}; ended {ended} open session(s)")
        if show:
            print(f"password: {password}")
        return 0

    if args.cmd == "link":
        # The upgrade path for a floor that had operator accounts before an
        # account could name a person. The account keeps its email and its
        # password and gains its person. Never re-minted - that is a new
        # password - and never invented from the account's name: a person
        # is created deliberately, on the desk, and a picker-made "Mei
        # Chen" would otherwise become a duplicate.
        if not existing:
            raise SystemExit(f"no account for {args.email}")
        if existing.role != OPERATOR:
            raise SystemExit(f"{args.email} is a manager, and a manager has no person to be")
        try:
            person_id = uuid.UUID(str(args.person))
        except (ValueError, TypeError):
            raise SystemExit(f"--person must be a people id, got {args.person!r}")
        person = await PersonRepository(session).get(person_id)
        if person is None:
            raise SystemExit(f"nobody has the id {person_id} - create the person on the desk first")
        taken = await accounts.by_person_id(person_id)
        if taken and taken.id != existing.id:
            raise SystemExit(f"{person.name} already signs in as {taken.email}")
        existing.person_id = person_id
        await session.commit()
        print(f"linked {existing.email} to {person.name}")
        return 0

    # manager | operator
    if existing:
        raise SystemExit(f"{args.email} already exists - use passwd or disable")
    role = MANAGER if args.cmd == "manager" else OPERATOR
    person_id = None
    person = None

    if role == OPERATOR:
        # The account says who. Where they sit is the roster the manager
        # pushes that morning, so nothing about a seat is taken here.
        try:
            person_id = uuid.UUID(str(args.person))
        except (ValueError, TypeError):
            raise SystemExit(f"--person must be a people id, got {args.person!r}")
        person = await PersonRepository(session).get(person_id)
        if person is None:
            raise SystemExit(
                f"nobody has the id {person_id} - create the person on the desk first")
        clash = await accounts.by_person_id(person_id)
        if clash:
            raise SystemExit(f"{person.name} already signs in as {clash.email}")

    password, show = _password(args.password, prompt=not args.generate)
    await accounts.add(Account(
        email=normalise_email(args.email), name=args.name, role=role,
        person_id=person_id, password_hash=hash_password(password),
        password_set_at=datetime.now(timezone.utc),
    ))
    await session.commit()
    print(f"created {role} {normalise_email(args.email)}"
          + (f" for {person.name}" if person else ""))
    if show:
        print(f"password: {password}")
        print("Hand it over and clear the scrollback. It is not stored anywhere "
              "readable - only its hash is.")
    return 0


def main() -> int:
    return asyncio.run(run(sys.argv[1:]))


async def run(argv: list[str]) -> int:
    """The tool, given its arguments. `main` hands it `sys.argv`; a test
    hands it a list, so what is tested is the real parser and not a
    copy of it."""
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def creds(sp, email=True):
        if email:
            sp.add_argument("--email", required=True)
        sp.add_argument("--password", help="leave out to be prompted")
        sp.add_argument("--generate", action="store_true",
                        help="generate one and print it, instead of prompting")

    sub.add_parser("list", help="every account and its state")

    for cmd in ("manager", "operator"):
        sp = sub.add_parser(cmd, help=f"create a {cmd} account")
        sp.add_argument("--name", required=True, help='display name, e.g. "Ruth Osei"')
        if cmd == "operator":
            sp.add_argument("--person", required=True,
                            help="the person this account is: the id from `people`, "
                                 "which a manager creates on the desk first")
        creds(sp)

    creds(sub.add_parser("passwd", help="set a new password and end open sessions"))
    sub.add_parser("disable", help="end access, keep the name").add_argument(
        "--email", required=True)
    link = sub.add_parser("link", help="give an existing operator account its person - "
                                       "the upgrade path, and it keeps the password")
    link.add_argument("--email", required=True)
    link.add_argument("--person", required=True, help="the id from `people`")
    invite = sub.add_parser("invite", help="mint an operator account with no password and "
                                           "mail a link to set one - needs the mail relay")
    invite.add_argument("--person", required=True, help="the id from `people`")

    args = p.parse_args(argv)
    if args.cmd in ("manager", "operator", "passwd") and not args.password \
            and not args.generate and not sys.stdin.isatty():
        # Prompting into a pipe reads EOF and sets an empty password.
        raise SystemExit("no terminal to prompt on - pass --password or --generate")
    if not hasattr(args, "person"):
        args.person = None
    if not hasattr(args, "email"):
        args.email = None
    return await _run(args)


if __name__ == "__main__":
    raise SystemExit(main())
