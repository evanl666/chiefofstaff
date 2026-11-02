"""A fake company "Docs" MCP server that requires OAuth sign-in, for live tests.

Its authorization server approves every request at once (as if the user clicked "Allow"), so a
test can complete the sign-in by simply following the authorization URL.

    uv run python scripts/fake_oauth_mcp.py --port 18902
"""

import argparse
import secrets
import threading
import time

import uvicorn
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.fastmcp import FastMCP
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

DOCS = {"42": "Q4 launch plan: ship the storefront redesign on Nov 18; owner Dana."}


class AutoApprove:
    """In-memory OAuth provider that approves every authorization request."""

    def __init__(self):
        self.clients: dict[str, OAuthClientInformationFull] = {}
        self.codes: dict[str, AuthorizationCode] = {}
        self.tokens: dict[str, AccessToken] = {}
        self.refresh: dict[str, RefreshToken] = {}
        self.authorized = 0

    async def get_client(self, client_id):
        return self.clients.get(client_id)

    async def register_client(self, client_info):
        self.clients[client_info.client_id] = client_info

    async def authorize(self, client, params: AuthorizationParams) -> str:
        code = secrets.token_urlsafe(16)
        self.codes[code] = AuthorizationCode(
            code=code,
            scopes=params.scopes or ["docs"],
            expires_at=time.time() + 300,
            client_id=client.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
        )
        self.authorized += 1
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    async def load_authorization_code(self, client, authorization_code):
        return self.codes.get(authorization_code)

    async def exchange_authorization_code(self, client, authorization_code):
        self.codes.pop(authorization_code.code, None)
        return self.issue(client.client_id, authorization_code.scopes, authorization_code.resource)

    async def load_refresh_token(self, client, refresh_token):
        return self.refresh.get(refresh_token)

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        self.refresh.pop(refresh_token.token, None)
        return self.issue(client.client_id, scopes or refresh_token.scopes, refresh_token.resource)

    async def load_access_token(self, token):
        return self.tokens.get(token)

    async def revoke_token(self, token):
        self.tokens.pop(getattr(token, "token", ""), None)

    def issue(self, client_id, scopes, resource):
        access, refresh = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        self.tokens[access] = AccessToken(
            token=access,
            client_id=client_id,
            scopes=scopes,
            expires_at=int(time.time()) + 3600,
            resource=resource,
        )
        self.refresh[refresh] = RefreshToken(
            token=refresh, client_id=client_id, scopes=scopes, resource=resource
        )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=3600,
            scope=" ".join(scopes),
            refresh_token=refresh,
        )


def build(port: int) -> tuple[FastMCP, AutoApprove]:
    provider = AutoApprove()
    base = f"http://127.0.0.1:{port}"
    server = FastMCP(
        "docs",
        host="127.0.0.1",
        port=port,
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=base,
            resource_server_url=f"{base}/mcp",
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=["docs"], default_scopes=["docs"]
            ),
            required_scopes=["docs"],
        ),
    )

    @server.tool(description="Read a company document by id")
    def read_doc(doc_id: str) -> str:
        return DOCS.get(doc_id, f"No document {doc_id}.")

    return server, provider


KEYCHAIN_SERVICE = "copilot-mcp-oauth"


def keychain_accounts() -> set[str]:
    """Accounts of Copilot's MCP sign-in entries in the macOS keychain (names only)."""
    import re
    import subprocess

    dump = subprocess.run(["security", "dump-keychain"], capture_output=True, text=True).stdout
    accounts = set()
    for block in dump.split("keychain: "):
        found = re.search(r'"acct"<blob>="([^"]+)"', block)
        if found and f'"svce"<blob>="{KEYCHAIN_SERVICE}"' in block:
            accounts.add(found.group(1))
    return accounts


def forget_sign_ins(port: int, before: set[str] | None = None) -> int:
    """Remove what Copilot stored for signing in to this test server.

    Copilot keeps a client registration file per server and keychain entries whose names do
    not reveal the server, so keychain entries that appeared since `before` are removed too.
    """
    import json
    import pathlib
    import subprocess

    def delete(account: str):
        subprocess.run(
            ["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account],
            capture_output=True,
        )

    removed = 0
    for path in pathlib.Path("~/.copilot/mcp-oauth-config").expanduser().glob("*.json"):
        try:
            server = json.loads(path.read_text()).get("serverUrl", "")
        except (OSError, ValueError):
            continue
        if server.startswith(f"http://127.0.0.1:{port}/"):
            delete(path.stem)
            path.unlink()
            removed += 1
    if before is not None:
        for account in keychain_accounts() - before:
            delete(account)
            removed += 1
    return removed


def serve_in_thread(port: int) -> AutoApprove:
    server, provider = build(port)
    config = uvicorn.Config(server.streamable_http_app(), port=port, log_level="warning")
    thread = threading.Thread(target=uvicorn.Server(config).run, daemon=True)
    thread.start()
    time.sleep(1)
    return provider


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18902)
    port = parser.parse_args().port
    server, _ = build(port)
    server.run(transport="streamable-http")
