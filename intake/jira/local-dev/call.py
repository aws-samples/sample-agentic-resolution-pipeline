#!/usr/bin/env python3
"""
Tiny CLI for poking the local MCP server.

Examples:
    ./call.py tools/list
    ./call.py call get_issue '{"issue_key":"OPS-100"}'
    ./call.py call create_issue '{"project_key":"OPS","issue_type":"Task","summary":"hi"}' \\
        --idem-key incident-test-1
    ./call.py < payloads/01_list_tools.json
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def post(payload: dict, idem_key: str | None, port: int) -> dict:
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if idem_key:
        headers["X-Idempotency-Key"] = idem_key
    url = f"http://localhost:{port}/mcp"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310 # nosemgrep: dynamic-urllib-use-detected
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"http_error": e.code, "body": e.read().decode()}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("verb", nargs="?", choices=["list", "tools/list", "call"])
    p.add_argument("name", nargs="?", help="tool name (when verb=call)")
    p.add_argument("args", nargs="?", default="{}", help="JSON args (when verb=call)")
    p.add_argument("--idem-key", default=None)
    p.add_argument("--port", type=int, default=int(os.getenv("PORT", "8081")))
    args = p.parse_args()

    # No args / piped JSON → read body from stdin.
    if not sys.stdin.isatty() and args.verb is None:
        payload = json.load(sys.stdin)
        out = post(payload, args.idem_key, args.port)
        print(json.dumps(out, indent=2))
        return

    if args.verb in ("list", "tools/list"):
        payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    elif args.verb == "call":
        if not args.name:
            p.error("call requires a tool name")
        try:
            tool_args = json.loads(args.args)
        except json.JSONDecodeError as e:
            p.error(f"args is not valid JSON: {e}")
        payload = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                   "params": {"name": args.name, "arguments": tool_args}}
    else:
        p.print_help()
        sys.exit(1)

    out = post(payload, args.idem_key, args.port)
    # The server wraps tool results in {"result": {"content": [{"type":"text","text": "<json string>"}]}}.
    # Pretty-print the inner text if present, raw response otherwise.
    if "result" in out and isinstance(out["result"], dict) and "content" in out["result"]:
        text = out["result"]["content"][0]["text"]
        try:
            print(json.dumps(json.loads(text), indent=2))
        except json.JSONDecodeError:
            print(text)
    else:
        print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
