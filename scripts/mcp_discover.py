"""List the MCP servers and tools the LiteLLM gateway exposes.

Use this before writing a card: the card needs the exact upstream tool name and
its parameter names, and guessing produces a card that registers but never works.

    python scripts/mcp_discover.py
    python scripts/mcp_discover.py --server ronaldo_sinequa
    python scripts/mcp_discover.py --server ronaldo_sinequa --json

Reads AO_LITELLM_PROXY_URL and AO_LITELLM_PROXY_API_KEY (or the _REF form) from
the environment / .env, so it talks to the gateway exactly as the app does.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import httpx

from alstom_ao.config import get_settings


def _headers(key: str) -> dict[str, str]:
    return {"x-litellm-api-key": f"Bearer {key}", "Accept": "application/json"}


def _describe(tool: dict) -> dict[str, str]:
    schema = tool.get("inputSchema") or tool.get("input_schema") or {}
    properties = (schema.get("properties") or {}) if isinstance(schema, dict) else {}
    required = set(schema.get("required") or []) if isinstance(schema, dict) else set()
    params = []
    for name, spec in properties.items():
        kind = (spec or {}).get("type", "any") if isinstance(spec, dict) else "any"
        params.append(f"{name}:{kind}{'*' if name in required else ''}")
    return {
        "name": str(tool.get("name", "")),
        "description": str(tool.get("description") or "").strip(),
        "parameters": ", ".join(params) or "-",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="List MCP servers and tools via the LiteLLM gateway.")
    parser.add_argument("--server", help="Only this server (name, alias or UUID).")
    parser.add_argument("--json", action="store_true", help="Print raw JSON.")
    parser.add_argument(
        "--key",
        help="Gateway key to use. Defaults to LITELLM_MASTER_KEY, then AO_LITELLM_PROXY_API_KEY.",
    )
    args = parser.parse_args()

    settings = get_settings()
    # The MCP routes resolve access grants from the gateway's database, which is
    # off when STORE_MODEL_IN_DB=False. In that setup only the master key works;
    # a virtual key answers 400 "No connected db." So prefer the master key.
    key, source = None, ""
    if args.key:
        key, source = args.key, "--key"
    elif os.environ.get("LITELLM_MASTER_KEY"):
        key, source = os.environ["LITELLM_MASTER_KEY"], "LITELLM_MASTER_KEY"
    else:
        key = settings.litellm_proxy_key()
        source = "AO_LITELLM_PROXY_API_KEY(_REF)"
    if not key:
        print(
            "No gateway key. Set LITELLM_MASTER_KEY (or AO_LITELLM_PROXY_API_KEY) in .env.",
            file=sys.stderr,
        )
        return 2
    print(f"key source: {source}", file=sys.stderr)

    base = settings.litellm_proxy_base
    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        try:
            response = client.get(f"{base}/v1/mcp/server", headers=_headers(key))
            servers = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            print(f"Cannot reach the gateway at {base}: {exc}", file=sys.stderr)
            return 1

        if response.status_code != 200 or not isinstance(servers, list):
            # The gateway can answer 400 {"error": {"message": "No connected db."}}
            # when the key has no MCP grant and the DB store is off, or while it is
            # still starting. Report the body and the likely cause rather than crash.
            print(f"GET /v1/mcp/server -> {response.status_code}: {response.text[:300]}", file=sys.stderr)
            if "no_db_connection" in response.text:
                print(
                    "hint: this gateway has STORE_MODEL_IN_DB=False, so MCP access grants\n"
                    "      cannot be resolved from the database. Use the master key:\n"
                    "      python scripts/mcp_discover.py --key $LITELLM_MASTER_KEY",
                    file=sys.stderr,
                )
            return 1

        if args.json:
            print(json.dumps(servers, indent=2))
        else:
            print(f"gateway: {base}")
            print(f"servers: {len(servers)}\n")
            for server in servers:
                print(
                    f"  {server.get('server_name')}  "
                    f"transport={server.get('transport')}  auth={server.get('auth_type')}"
                )
                print(f"    url: {server.get('url')}")
                if server.get("health_check_error"):
                    print(f"    health error: {server['health_check_error']}")
            print()

        params = {"server_id": args.server} if args.server else None
        try:
            response = client.get(f"{base}/mcp-rest/tools/list", params=params, headers=_headers(key))
        except httpx.HTTPError as exc:
            print(f"tools/list failed: {exc}", file=sys.stderr)
            return 1

        if response.status_code != 200:
            print(f"tools/list -> {response.status_code}: {response.text[:400]}", file=sys.stderr)
            return 1

        body = response.json()
        tools = [t for t in body.get("tools") or [] if isinstance(t, dict)]
        if body.get("error"):
            print(f"gateway reports: {body['error']} - {body.get('message')}", file=sys.stderr)
            outcomes = body.get("server_outcomes") or {}
            for name, outcome in outcomes.items():
                print(f"  {name}: {outcome}", file=sys.stderr)

        if args.json:
            print(json.dumps(tools, indent=2))
            return 0 if tools else 1

        if not tools:
            print("no tools returned.")
            return 1

        print(f"tools ({len(tools)}):")
        for tool in tools:
            info = _describe(tool)
            server_name = ((tool.get("mcp_info") or {}).get("server_name")) or "?"
            print(f"\n  {info['name']}    [server: {server_name}]")
            if info["description"]:
                print(f"    {info['description'][:160]}")
            print(f"    parameters: {info['parameters']}")

        print("\nCard metadata for the first tool whose parameters fit your query:")
        for tool in tools:
            schema = tool.get("inputSchema") or tool.get("input_schema") or {}
            props = list((schema.get("properties") or {}).keys()) if isinstance(schema, dict) else []
            if not props:
                continue
            print(
                "\n  \"metadata\": {\n"
                f"    \"mcp_server_id\": \"{args.server or '<server_name>'}\",\n"
                f"    \"mcp_tool\": \"{tool.get('name')}\",\n"
                f"    \"mcp_argument\": \"{props[0]}\"\n"
                "  }"
            )
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
