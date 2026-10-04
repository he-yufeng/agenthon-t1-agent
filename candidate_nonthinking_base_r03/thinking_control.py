"""Single-factor, approved House request option over the frozen core (MIT)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import engine as core

BASE_SHA256 = "1c85bae327299e6eeacf7f1c788dfd5bf93d8f3c67a45e723280f37aaa4bef3a"


class NonThinkingHouseClient(core.OpenAICompatibleClient):
    def __init__(self, *args, **kwargs):
        if hashlib.sha256(Path(core.__file__).read_bytes()).hexdigest() != BASE_SHA256:
            raise core.EndpointError("request control requires exact frozen core")
        super().__init__(*args, **kwargs)

    def _complete_once(self, request, request_deadline):
        try:
            body = json.loads(request.data)
        except (ValueError, TypeError, UnicodeDecodeError):
            raise core.EndpointError("request control received invalid JSON") from None
        if not isinstance(body, dict):
            raise core.EndpointError("request control requires an object")
        existing = body.get("chat_template_kwargs")
        if existing is not None and existing != {"enable_thinking": False}:
            raise core.EndpointError("combined thinking controls refused")
        body["chat_template_kwargs"] = {"enable_thinking": False}
        # Request.data's setter invalidates an old Content-Length. All other
        # headers, URL, proxy handling and the admitted-attempt deadline remain.
        request.data = json.dumps(body).encode("utf-8")
        return super()._complete_once(request, request_deadline)
