"""Serve a local OpenAI-compatible provider for aiming RayChat at something real."""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, ClassVar

from raychat.type_support import override
from raychat.validation import (
    ConfigurationError,
    array_field,
    configuration_fields,
    json_object,
    text_field,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from io import BufferedIOBase

_MAX_REQUEST_BYTES = 1 << 20
_DEFAULT_MODELS = ("stub/small", "stub/large", "stub/reasoning")


class _Arguments(argparse.Namespace):
    host: str
    port: int
    prefix: str
    token: str
    models: str
    hide_catalog: bool


def _write(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def _error(message: str, kind: str) -> dict[str, object]:
    return {"error": {"message": message, "type": kind}}


def _catalog_body(models: Sequence[str]) -> dict[str, object]:
    return {
        "object": "list",
        "data": [
            {"id": name, "object": "model", "created": 0, "owned_by": "provider-stub"}
            for name in models
        ],
    }


def _requested(document: object) -> tuple[str, str]:
    fields = configuration_fields(document, "request")
    model = text_field(fields.get("model"), "request.model")
    entries = array_field(fields.get("messages"), "request.messages")
    if not entries:
        message = "request.messages must not be empty."
        raise ConfigurationError(message)
    last = configuration_fields(entries[-1], "request.messages[-1]")
    return model, text_field(last.get("content"), "request.messages[-1].content")


def _request_body(declared: str, stream: BufferedIOBase) -> bytes:
    """Read exactly the declared number of request bytes, within a fixed bound.

    Returns
    -------
    bytes
        The request body.

    Raises
    ------
    ValueError
        If the declared length is absent, zero, or beyond the accepted bound.

    """
    size = int(declared)
    if not 0 < size <= _MAX_REQUEST_BYTES:
        message = "Request size is out of bounds."
        raise ValueError(message)
    return stream.read(size)


def _completion_body(model: str, prompt: str) -> dict[str, object]:
    answer = (
        f"This is the local provider stub answering as {model}. You said {prompt!r}."
    )
    asked, replied = len(prompt.split()), len(answer.split())
    return {
        "id": "chatcmpl-" + secrets.token_hex(12),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": answer},
                "finish_reason": "stop",
            },
        ],
        "usage": {
            "prompt_tokens": asked,
            "completion_tokens": replied,
            "total_tokens": asked + replied,
        },
    }


class StubHandler(BaseHTTPRequestHandler):
    """Serve one catalog and one credential, configured by a subclass."""

    protocol_version = "HTTP/1.1"
    catalog: ClassVar[tuple[str, ...]] = ()
    credential: ClassVar[str] = ""
    catalog_path: ClassVar[str] = "/v1/models"
    chat_path: ClassVar[str] = "/v1/chat/completions"
    hide_catalog: ClassVar[bool] = False
    quiet: ClassVar[bool] = False
    answered: int = 0

    @override
    def log_message(self, _format: str, *_args: object) -> None:
        """Report each request on one line, unless the caller asked for quiet."""
        if not self.quiet:
            _write(f"  {self.command} {self.path} -> {self.answered}")

    def reply(self, status: HTTPStatus, body: object) -> None:
        """Send one JSON document and record the status for the access log."""
        self.answered = int(status)
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def refused(self, path: str) -> bool:
        """Answer whether the credential or the route already failed.

        Returns
        -------
        bool
            True when a response has been sent and nothing more is needed.

        """
        if self.headers.get("Authorization", "") != "Bearer " + self.credential:
            self.reply(
                HTTPStatus.UNAUTHORIZED,
                _error("Invalid credential.", "auth_error"),
            )
            return True
        if self.path != path:
            self.reply(
                HTTPStatus.NOT_FOUND,
                _error("Unknown route.", "not_found"),
            )
            return True
        return False

    def do_GET(self) -> None:
        """Answer the model catalog, or refuse it to imitate a bare server."""
        if self.refused(self.catalog_path):
            return
        if self.hide_catalog:
            self.reply(
                HTTPStatus.NOT_FOUND,
                _error("No model catalog.", "not_found"),
            )
            return
        self.reply(HTTPStatus.OK, _catalog_body(self.catalog))

    def do_POST(self) -> None:
        """Answer a chat completion with a deterministic reply and usage."""
        if self.refused(self.chat_path):
            return
        try:
            raw = _request_body(self.headers.get("Content-Length", "0"), self.rfile)
            model, prompt = _requested(json_object(raw))
        except (ConfigurationError, TypeError, ValueError) as exc:
            self.reply(
                HTTPStatus.BAD_REQUEST,
                _error(str(exc), "invalid_request"),
            )
            return
        self.reply(HTTPStatus.OK, _completion_body(model, prompt))


def handler(
    models: Sequence[str],
    token: str,
    prefix: str,
    *,
    hide: bool,
    silent: bool = False,
) -> type[BaseHTTPRequestHandler]:
    """Bind one catalog, credential and route prefix to a handler class.

    Returns
    -------
    type[BaseHTTPRequestHandler]
        A handler serving the chat and model endpoints under the prefix.

    """

    class Handler(StubHandler):
        catalog = tuple(models)
        credential = token
        catalog_path = prefix + "/models"
        chat_path = prefix + "/chat/completions"
        hide_catalog = hide
        quiet = silent

    return Handler


def serve(arguments: _Arguments) -> None:
    """Run the stub until interrupted, announcing how to point RayChat at it."""
    models = tuple(name.strip() for name in arguments.models.split(",") if name.strip())
    server = ThreadingHTTPServer(
        (arguments.host, arguments.port),
        handler(
            models,
            arguments.token,
            arguments.prefix,
            hide=arguments.hide_catalog,
        ),
    )
    base = f"http://{arguments.host}:{server.server_port}{arguments.prefix}"
    _write("Local provider stub listening.")
    _write(f"  RAYCHAT_BASE_URL={base}")
    _write(f"  RAYCHAT_AUTH_TOKEN={arguments.token}")
    _write(f"  RAYCHAT_MODEL={models[0] if models else '<none advertised>'}")
    _write(f"  catalog: {'404 (bare server)' if arguments.hide_catalog else 'served'}")
    _write(f"  models : {', '.join(models) if models else '(empty)'}")
    _write("Press Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _write("Stopped.")
    finally:
        server.shutdown()
        server.server_close()


def main() -> None:
    """Serve the stub at the address selected by the command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--prefix", default="/v1")
    parser.add_argument("--token", default="stub-token")
    parser.add_argument("--models", default=",".join(_DEFAULT_MODELS))
    parser.add_argument(
        "--hide-catalog",
        action="store_true",
        help="Answer 404 for the model catalog, as a bare server would.",
    )
    arguments = _Arguments()
    parser.parse_args(namespace=arguments)
    serve(arguments)


if __name__ == "__main__":
    main()
