"""Refusals, in the shape an OpenAI client already understands.

Every refusal carries a LibreRun ``code`` in the OpenAI error envelope,
because the caller is usually somebody else's SDK: an agent framework
reading ``OPENAI_BASE_URL`` gets a message it can print and a code the
platform's own tests and docs name.

Nothing here ever echoes a value. A refusal over PII names the *path*
that carried it and never the text, or the refusal would leak exactly
what it exists to keep in the box.
"""
from __future__ import annotations

from fastapi import HTTPException
from fastapi.responses import JSONResponse


class GatewayError(HTTPException):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        error_type: str = "invalid_request_error",
        param: str | None = None,
    ) -> None:
        super().__init__(status_code=status_code, detail=message)
        self.code = code
        self.message = message
        self.error_type = error_type
        self.param = param

    def body(self) -> dict:
        error: dict = {
            "message": self.message,
            "type": self.error_type,
            "code": self.code,
        }
        if self.param is not None:
            error["param"] = self.param
        return {"error": error}


def unauthorized(code: str, message: str) -> GatewayError:
    return GatewayError(401, code, message, error_type="authentication_error")


def forbidden(code: str, message: str) -> GatewayError:
    return GatewayError(403, code, message, error_type="permission_error")


def bad_request(code: str, message: str, *, param: str | None = None) -> GatewayError:
    return GatewayError(400, code, message, param=param)


def unavailable(code: str, message: str) -> GatewayError:
    """A dependency this process needs is not there. 503, so an SDK
    retries rather than treating it as the caller's mistake."""
    return GatewayError(503, code, message, error_type="service_unavailable")


async def gateway_error_handler(_request, exc: GatewayError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=exc.body())
