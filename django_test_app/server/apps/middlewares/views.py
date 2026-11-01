from http import HTTPStatus
from typing import Final, final

import pydantic
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie

from dmr import Body, Controller, ResponseSpec
from dmr.decorators import wrap_middleware
from dmr.errors import ErrorModel, format_error
from dmr.idempotency import Idempotency
from dmr.plugins.pydantic import PydanticSerializer
from dmr.response import build_response
from server.apps.middlewares.middleware import (
    add_request_id_middleware,
    custom_header_middleware,
    rate_limit_middleware,
)

_MESSAGE_KEY: Final = 'message'
_CHARGED_STATUS: Final = 'charged'

# Simulates a database table of charge records,
# each retry with the same idempotency key must not add a new row here:
_created_charges: list[dict[str, object]] = []


def reset_charges() -> None:
    """Clear the charge ledger, used in tests."""
    _created_charges.clear()


def charge_count() -> int:
    """Return how many charge records currently exist."""
    return len(_created_charges)


idempotency = Idempotency(serializer=PydanticSerializer)


@final
class _RequestWithID(HttpRequest):
    request_id: str


@wrap_middleware(
    csrf_protect,
    ResponseSpec(
        return_type=dict[str, list[dict[str, str]]],
        status_code=HTTPStatus.FORBIDDEN,
    ),
)
def csrf_protect_json(response: HttpResponse) -> HttpResponse:
    return build_response(
        PydanticSerializer,
        raw_data=format_error('CSRF verification failed. Request aborted.'),
        status_code=HTTPStatus.FORBIDDEN,
    )


@wrap_middleware(
    ensure_csrf_cookie,
    ResponseSpec(
        return_type=dict[str, str],
        status_code=HTTPStatus.OK,
    ),
)
def ensure_csrf_cookie_json(response: HttpResponse) -> HttpResponse:
    return response


@wrap_middleware(
    custom_header_middleware,
    ResponseSpec(
        return_type=dict[str, str],
        status_code=HTTPStatus.OK,
    ),
)
def custom_header_json(response: HttpResponse) -> HttpResponse:
    return response


@wrap_middleware(
    rate_limit_middleware,
    ResponseSpec(
        return_type=ErrorModel,
        status_code=HTTPStatus.TOO_MANY_REQUESTS,
    ),
)
def rate_limit_json(response: HttpResponse) -> HttpResponse:
    return response


@wrap_middleware(
    add_request_id_middleware,
    ResponseSpec(
        return_type=dict[str, str],
        status_code=HTTPStatus.OK,
    ),
)
def add_request_id_json(response: HttpResponse) -> HttpResponse:
    """Pass through response - ``request_id`` is added automatically."""
    return response


@wrap_middleware(
    login_required,
    ResponseSpec(
        return_type=dict[str, str],
        status_code=HTTPStatus.FOUND,
    ),
    ResponseSpec(  # Uses for proxy authed response with HTTPStatus.OK
        return_type=dict[str, str],
        status_code=HTTPStatus.OK,
    ),
    ResponseSpec(
        return_type=ErrorModel,
        status_code=HTTPStatus.UNAUTHORIZED,
    ),
)
def login_required_json(response: HttpResponse) -> HttpResponse:
    """Convert Django's ``login_required`` redirect to JSON 401 response."""
    if response.status_code == HTTPStatus.FOUND:
        return build_response(
            PydanticSerializer,
            raw_data=format_error(
                'Authentication credentials were not provided',
            ),
            status_code=HTTPStatus.UNAUTHORIZED,
        )
    return response


@wrap_middleware(
    idempotency,
    ResponseSpec(
        return_type=ErrorModel,
        status_code=HTTPStatus.CONFLICT,
    ),
)
def idempotent_json(response: HttpResponse) -> HttpResponse:
    """Pass through idempotency replays and conflicts."""
    return response


@final
class _UserInput(pydantic.BaseModel):
    email: str
    age: int = pydantic.Field(strict=True)


@final
class _ChargeInput(pydantic.BaseModel):
    order_id: str
    amount: int = pydantic.Field(strict=True)


@final
class _ChargeResult(pydantic.BaseModel):
    record_id: int
    order_id: str
    amount: int
    status: str


@final
@idempotent_json
class ChargeController(Controller[PydanticSerializer]):
    """Creates a charge record, protected by the idempotency middleware."""

    responses = idempotent_json.responses

    def post(self, parsed_body: Body[_ChargeInput]) -> _ChargeResult:
        record = {
            'record_id': len(_created_charges) + 1,
            'order_id': parsed_body.order_id,
            'amount': parsed_body.amount,
            'status': _CHARGED_STATUS,
        }
        _created_charges.append(record)
        return _ChargeResult(**record)


@final
@idempotent_json
class AsyncChargeController(Controller[PydanticSerializer]):
    """Async version of :class:`ChargeController`."""

    responses = idempotent_json.responses

    async def post(self, parsed_body: Body[_ChargeInput]) -> _ChargeResult:
        record = {
            'record_id': len(_created_charges) + 1,
            'order_id': parsed_body.order_id,
            'amount': parsed_body.amount,
            'status': _CHARGED_STATUS,
        }
        _created_charges.append(record)
        return _ChargeResult(**record)


@final
@ensure_csrf_cookie_json
class CsrfTokenController(Controller[PydanticSerializer]):
    """Controller to obtain CSRF token."""

    responses = ensure_csrf_cookie_json.responses

    def get(self) -> dict[str, str]:
        """GET endpoint that ensures CSRF cookie is set."""
        return {_MESSAGE_KEY: 'CSRF token set'}


@final
@csrf_protect_json
class CsrfProtectedController(Controller[PydanticSerializer]):
    # Just add responses from middleware
    responses = csrf_protect_json.responses

    def post(self, parsed_body: Body[_UserInput]) -> _UserInput:
        return parsed_body


@final
@csrf_protect_json
class AsyncCsrfProtectedController(Controller[PydanticSerializer]):
    # Just add responses from middleware
    responses = csrf_protect_json.responses

    async def post(self, parsed_body: Body[_UserInput]) -> _UserInput:
        return parsed_body


@final
@custom_header_json
class CustomHeaderController(Controller[PydanticSerializer]):
    """Controller with custom header middleware."""

    responses = custom_header_json.responses

    def get(self) -> dict[str, str]:
        """GET endpoint that returns simple data."""
        return {_MESSAGE_KEY: 'Success'}


@final
@rate_limit_json
class RateLimitedController(Controller[PydanticSerializer]):
    """Controller with rate limiting middleware."""

    responses = rate_limit_json.responses

    def post(self, parsed_body: Body[_UserInput]) -> _UserInput:
        """POST endpoint with rate limiting."""
        return parsed_body


@final
@add_request_id_json
class RequestIdController(Controller[PydanticSerializer]):
    """Controller that uses ``request_id`` added by middleware."""

    responses = add_request_id_json.responses

    request: _RequestWithID

    def get(self) -> dict[str, str]:
        """GET endpoint that returns request_id from modified request."""
        return {
            'request_id': self.request.request_id,
            'message': 'Request ID tracked',
        }


@final
@login_required_json
class LoginRequiredController(Controller[PydanticSerializer]):
    """Controller that uses Django's ``login_required`` decorator.

    Demonstrates wrapping Django's built-in authentication decorators.
    Converts 302 redirect to JSON 401 response for REST API compatibility.
    """

    responses = login_required_json.responses

    def get(self) -> dict[str, str]:
        """GET endpoint that requires Django authentication."""
        # Access Django's authenticated user
        user = self.request.user
        username = user.username if user.is_authenticated else 'anonymous'

        return {
            'username': username,
            _MESSAGE_KEY: 'Successfully accessed protected resource',
        }
