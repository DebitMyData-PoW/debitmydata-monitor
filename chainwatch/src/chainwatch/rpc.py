"""A small JSON-RPC client for the monitoring server's own DebitMyData node (read-only calls).

The node runs on this server with RPC on 127.0.0.1 only. The chain watch signs in with the node's
cookie (readable by the debitmydata group: -rpccookieperms=group), read again on every call, so a
node restart (a new cookie) needs nothing. A user and password work too (DMDW_RPC_USER).
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

# The only calls the chain watch makes: all of them read.
ALLOWED = frozenset(
    {
        "getblockchaininfo",
        "getnetworkinfo",
        "getblockcount",
        "getbestblockhash",
        "getblockhash",
        "getblockheader",
        "getblock",
        "getchaintips",
        "getconnectioncount",
        "validateaddress",
    }
)


class RpcError(RuntimeError):
    """The node answered with an error."""


class RpcUnavailable(RuntimeError):
    """The node could not be reached (stopped, starting, or the cookie is missing)."""


class Rpc:
    def __init__(
        self, url: str, cookie: str = "", user: str = "", password: str = "", timeout: float = 30
    ) -> None:
        self.url, self.cookie, self.user, self.password = url, cookie, user, password
        self.timeout = timeout
        self._id = 0

    def _auth(self) -> str:
        if self.user:
            pair = f"{self.user}:{self.password}"
        else:
            try:
                pair = Path(self.cookie).read_text().strip()
            except OSError as exc:
                raise RpcUnavailable(f"the node's cookie cannot be read: {exc}") from exc
        return "Basic " + base64.b64encode(pair.encode()).decode()

    def _post(self, payload: Any) -> Any:
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Authorization": self._auth()},
            method="POST",
        )
        # Amounts stay exact: decimal numbers are read as Decimal, never as floats.
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read(), parse_float=Decimal)
        except urllib.error.HTTPError as exc:
            # The node answers errors with HTTP 500 and a JSON body.
            try:
                return json.loads(exc.read(), parse_float=Decimal)
            except (ValueError, OSError):
                raise RpcUnavailable(f"HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise RpcUnavailable(str(exc)) from exc

    def call(self, method: str, *params: Any) -> Any:
        if method not in ALLOWED:
            raise RpcError(f"{method} is not a call the chain watch makes")
        self._id += 1
        answer = self._post({"jsonrpc": "1.0", "id": self._id, "method": method, "params": params})
        if isinstance(answer, list) or not isinstance(answer, dict):
            raise RpcError("unexpected answer from the node")
        if answer.get("error"):
            raise RpcError(f"{method}: {answer['error'].get('message', answer['error'])}")
        return answer.get("result")

    def batch(self, calls: Sequence[tuple[str, Sequence[Any]]]) -> list[Any]:
        """Several calls in one request; the answers in the same order."""
        if not calls:
            return []
        for method, _ in calls:
            if method not in ALLOWED:
                raise RpcError(f"{method} is not a call the chain watch makes")
        first = self._id + 1
        payload = [
            {"jsonrpc": "1.0", "id": first + i, "method": method, "params": list(params)}
            for i, (method, params) in enumerate(calls)
        ]
        self._id += len(calls)
        answers = self._post(payload)
        if not isinstance(answers, list):
            raise RpcError("unexpected answer from the node")
        by_id = {a.get("id"): a for a in answers if isinstance(a, dict)}
        results = []
        for i, (method, _) in enumerate(calls):
            answer = by_id.get(first + i)
            if answer is None:
                raise RpcError(f"{method}: no answer")
            if answer.get("error"):
                raise RpcError(f"{method}: {answer['error'].get('message', answer['error'])}")
            results.append(answer.get("result"))
        return results
