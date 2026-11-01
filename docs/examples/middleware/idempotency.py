from http import HTTPStatus

from django.http import HttpResponse
from pydantic import BaseModel

from dmr import Body, Controller, ResponseSpec
from dmr.decorators import wrap_middleware
from dmr.errors import ErrorModel
from dmr.idempotency import Idempotency
from dmr.plugins.pydantic import PydanticSerializer

idempotency = Idempotency(serializer=PydanticSerializer)


@wrap_middleware(
    idempotency,
    ResponseSpec(
        return_type=ErrorModel,
        status_code=HTTPStatus.CONFLICT,
    ),
)
def idempotent_json(response: HttpResponse) -> HttpResponse:
    """Replays and conflicts are already proper JSON responses."""
    return response


class Charge(BaseModel):
    """A single charge request."""

    order_id: str
    amount: int


@idempotent_json
class ChargeController(Controller[PydanticSerializer]):
    """Charge endpoint that safely handles client retries."""

    responses = idempotent_json.responses

    def post(self, charge: Body[Charge]) -> dict[str, object]:
        # This runs at most once per unique `Idempotency-Key`:
        return {'order_id': charge.order_id, 'status': 'charged'}
