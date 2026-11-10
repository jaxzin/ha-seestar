#!/usr/bin/env python3
"""Call the Home Assistant Supervisor API through the Core WebSocket.

The long-lived HA token cannot call `/api/hassio/*` over REST, but the
`supervisor/api` WebSocket command works. Needs `websocket-client`.

Secrets only ever travel on stdin (never argv, never a file):

    line 1: HA long-lived token
    line 2: JSON request {"endpoint": "/addons/<slug>/info", "method": "get",
                          "data": {...optional...}}
    line 3: (optional) base64 PEM; injected as data.options.interop_pem

Usage (run where the LAN is reachable, e.g. over ssh to heimdall):

    HA_WS_URL=wss://homeassistant.jaxzin.casa/api/websocket \\
      ha-supervisor-api.py < request.stdin

Prints the JSON result with every non-empty password/token/secret/key/pem value masked.
"""
import json
import os
import sys

REDACT = "interop_pem"  # key the PEM is injected under
_SENSITIVE = ("pem", "password", "token", "secret", "key")


def _redact(value):
    """Recursively mask any non-empty value whose key looks sensitive."""
    if isinstance(value, dict):
        return {
            k: ("<redacted>" if v and any(s in str(k).lower() for s in _SENSITIVE) else _redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def main() -> int:
    import websocket  # websocket-client

    lines = sys.stdin.read().split("\n")
    token = lines[0].strip()
    request = json.loads(lines[1])
    pem_b64 = lines[2].strip() if len(lines) > 2 else ""
    if pem_b64:
        request.setdefault("data", {}).setdefault("options", {})[REDACT] = pem_b64

    url = os.environ.get("HA_WS_URL", "wss://homeassistant.jaxzin.casa/api/websocket")
    ws = websocket.create_connection(url, timeout=30)
    try:
        json.loads(ws.recv())  # auth_required
        ws.send(json.dumps({"type": "auth", "access_token": token}))
        if json.loads(ws.recv()).get("type") != "auth_ok":
            print("auth failed", file=sys.stderr)
            return 1
        ws.send(json.dumps({"id": 1, "type": "supervisor/api", **request}))
        reply = json.loads(ws.recv())
    finally:
        ws.close()
    print(json.dumps(_redact(reply), indent=2))
    return 0 if reply.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
