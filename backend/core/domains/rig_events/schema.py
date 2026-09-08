"""The event envelope, as Pydantic.

This is the Python half of a contract that also exists in JavaScript.
`packages/schema/event.schema.json` is the canonical spec and
`packages/schema/fixtures/` is what proves the two halves still agree -
the test suite loads every fixture and asserts this model accepts it. If
either side stops accepting a fixture, CI says so on the side that broke.
"""

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

HHMM = Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]

BUCKETS: dict[str, set[str]] = {
    "episodes": {"episode_saved", "episode_discarded"},
    "rig_shift_checks": {
        "shift_check", "fault_opened", "fault_reclassified",
        "fault_closed", "fault_cancelled",
    },
    "rig_downtime_events": {"rig_down", "rig_up"},
    "rig_productivity_blocks": {"stint_ended"},
    "sessions": {"session_ended"},
}


class EventEnvelope(BaseModel):
    """One event from one rig.

    `data` is deliberately open. The far side ignores fields it does not
    know, which is what lets the rig ship a new measurement before the
    server reads it - and it is why `data` carries measurements only,
    never a conclusion. A stored percentage cannot be corrected without
    re-running the floor; the numbers it was made of can.
    """

    model_config = ConfigDict(extra="forbid")

    event_id: UUID = Field(alias="eventId")
    seq: int = Field(ge=0)
    at: datetime

    rig_id: str = Field(alias="rigId", min_length=1)
    shift_date: str = Field(alias="shiftDate", pattern=r"^\d{4}-\d{2}-\d{2}$")
    shift_label: str = Field(alias="shiftLabel", min_length=1)

    # Null when nothing is scheduled - a rig on standby still reports.
    turn_from: HHMM | None = Field(default=None, alias="turnFrom")
    operator_id: str | None = Field(default=None, alias="operatorId")
    # operator_id is a seat - "op-" + group + slot - so the same id is a
    # different person on a cover day, and no other table keeps a name to
    # tell them apart.
    #
    # Optional, and it must stay optional. Every event already queued on a
    # rig when this ships was written without it, and a rig that is
    # refused does not retry: rig.js drops a 422 batch from the outbox and
    # forgets it from the journal. Demanding this field would destroy that
    # work rather than delay it.
    operator_name: str | None = Field(default=None, alias="operatorName", max_length=128)
    # The person, as distinct from the seat and from the name. `people.id`,
    # minted once when a manager adds somebody and carried into every
    # seat they ever work - so "who recorded this" has one answer across
    # months, which neither the seat nor a name can give.
    #
    # Optional, and it must stay optional, for exactly the reason above:
    # no rig sends this until the release after the server accepts it.
    #
    # Not checked against `people` at ingest, and there is no foreign key.
    # An event is a fact about what the rig was told; refusing it because
    # a table elsewhere disagrees would destroy work, not correct it. And
    # a restore re-POSTs the ledger through this route - people are
    # outside the ledger, like schedules, so a key would make a restore
    # order-dependent and a missing row into lost events.
    person_id: UUID | None = Field(default=None, alias="personId")

    bucket: Literal[
        "episodes", "rig_shift_checks", "rig_downtime_events",
        "rig_productivity_blocks", "sessions",
    ]
    event: str
    data: dict[str, Any]

    @field_validator("at")
    @classmethod
    def must_carry_a_zone(cls, v: datetime) -> datetime:
        """A timestamp without an offset is unsortable across twelve rigs."""
        if v.tzinfo is None:
            raise ValueError("at must carry a timezone offset")
        return v

    @field_validator("event")
    @classmethod
    def event_must_be_known(cls, v: str) -> str:
        if not any(v in events for events in BUCKETS.values()):
            raise ValueError(f"unknown event {v!r}")
        return v

    def model_post_init(self, _ctx) -> None:
        if self.event not in BUCKETS[self.bucket]:
            raise ValueError(
                f"event {self.event!r} does not belong in bucket {self.bucket!r}"
            )


class EventBatch(BaseModel):
    """What POST /events accepts. All-or-nothing: a half-accepted batch is
    worse than a rejected one, exactly as the schedule push already works.

    The example below is what the published contract shows. It is one
    take: an operator recorded 92 seconds and scored it 4. Sending it a
    second time returns `accepted: 0`.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "events": [{
                    "eventId": "9f1c7a2e-3b44-4c8d-9a11-6d2e5f0b7c31",
                    "seq": 4417,
                    "at": "2026-08-24T09:01:02.881Z",
                    "rigId": "RIG-03",
                    "shiftDate": "2026-08-24",
                    "shiftLabel": "Morning",
                    "turnFrom": "09:00",
                    "operatorId": "op-a4",
                    "bucket": "episodes",
                    "event": "episode_saved",
                    "data": {
                        "episodeId": "11111111-1111-4111-8111-111111111111",
                        "durationSecs": 92,
                        "score": 4,
                    },
                }]
            }
        }
    )

    events: list[EventEnvelope] = Field(min_length=1, max_length=1000)


class IngestResult(BaseModel):
    accepted: int
    duplicates: int
    cursor: int
