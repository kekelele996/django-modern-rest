from typing import Annotated, final

import pydantic

#: Ticket prices and capacity limits must stay positive.
PositivePrice = Annotated[float, pydantic.Field(gt=0)]
PositiveCapacity = Annotated[int, pydantic.Field(gt=0)]


@final
class TicketSettings(pydantic.BaseModel):
    """The ``tickets`` section of an activity config document."""

    model_config = pydantic.ConfigDict(extra='forbid')

    #: Single ticket price (Chinese: ``门票价格``).
    price: PositivePrice


@final
class RegistrationSettings(pydantic.BaseModel):
    """The ``registration`` section of an activity config document."""

    model_config = pydantic.ConfigDict(extra='forbid')

    #: Sign-up switch (Chinese: ``报名开关``).
    enabled: bool
    #: Maximum number of attendees (Chinese: ``人数上限``).
    capacity: PositiveCapacity


@final
class ActivityConfig(pydantic.BaseModel):
    """
    Full activity config document used to validate a merged result.

    Every section is optional on its own: a section can be missing from
    a sparse document, but whenever a section is present all of its
    required fields must be valid.
    """

    model_config = pydantic.ConfigDict(extra='forbid')

    tickets: TicketSettings | None = None
    registration: RegistrationSettings | None = None


@final
class ActivityConfigResponseSchema(pydantic.BaseModel):
    """The effective config document after a partial save."""

    config: dict[str, object]
