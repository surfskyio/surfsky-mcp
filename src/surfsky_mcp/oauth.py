import secrets
import time
from typing import NamedTuple
from urllib.parse import quote, urlencode

import httpx2
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response

from .sessions import Hosted

SCOPE = "surfsky"
ACCESS_PREFIX = "mcp_"
ACCESS_TTL = 3600
CODE_TTL = 60
PENDING_TTL = 600
MAX_PENDING = 10_000


class Grant(AuthorizationCode):
    api_token: str
    refresh_token: str


class IssuedToken(NamedTuple):
    expires_at: float
    client_id: str
    connected_app_id: int
    api_token: str


class Provider(OAuthAuthorizationServerProvider[Grant, RefreshToken, AccessToken]):
    def __init__(
        self,
        accounts: Hosted,
        mcp_url: str,
        consent_url: str,
        web_app: httpx2.AsyncClient,
    ) -> None:
        self._accounts = accounts
        self.issuer = mcp_url
        self.resource = f"{mcp_url}/mcp"
        self._consent_url = consent_url
        self._web_app = web_app
        self._hosts: dict[str, OAuthClientInformationFull] = {}
        self._pending: dict[str, tuple[float, str, AuthorizationParams]] = {}
        self._grants: dict[str, Grant] = {}
        self._tokens: dict[str, IssuedToken] = {}

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        host = self._hosts.get(client_id)
        if host is None:
            response = await self._web_app.get(
                f"/internal/mcp/hosts/{quote(client_id, safe='')}"
            )
            if response.status_code == 404:
                return None
            response.raise_for_status()
            host = self._hosts[client_id] = OAuthClientInformationFull.model_validate(
                response.json()
            )
        return host

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        response = await self._web_app.post(
            "/internal/mcp/hosts",
            json=client_info.model_dump(mode="json", exclude_none=True),
        )
        if response.status_code in (400, 413):
            raise RegistrationError("invalid_client_metadata")
        response.raise_for_status()

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        now = time.time()
        self._pending = {k: v for k, v in self._pending.items() if v[0] > now}
        if len(self._pending) >= MAX_PENDING:
            del self._pending[next(iter(self._pending))]  # the oldest
        request_id = secrets.token_urlsafe(32)
        self._pending[request_id] = (now + PENDING_TTL, client.client_id, params)
        query = urlencode(
            {
                "request": request_id,
                "client_id": client.client_id,
                "redirect_uri": str(params.redirect_uri),
            }
        )
        return f"{self._consent_url}?{query}"

    async def callback(self, request: Request) -> Response:
        expires, client_id, params = self._pending.pop(
            request.query_params.get("request", ""), (0.0, "", None)
        )
        if params is None or expires < time.time():
            return PlainTextResponse(
                "This login expired. Start again from your app.", status_code=400
            )
        redirect_uri = str(params.redirect_uri)

        def redirect(**query: str | None) -> Response:
            return RedirectResponse(
                construct_redirect_uri(redirect_uri, **query, state=params.state),
                status_code=302,
                headers={"Cache-Control": "no-store"},
            )

        code = request.query_params.get("code")
        if not code:
            return redirect(error="access_denied")
        try:
            response = await self._web_app.post(
                "/internal/mcp/redeem",
                json={"code": code, "client_id": client_id, "redirect_uri": redirect_uri},
            )
        except httpx2.HTTPError:
            return redirect(error="server_error")
        if response.status_code == 404:
            return redirect(error="access_denied")
        if response.status_code != 200:
            return redirect(error="server_error")
        tokens = response.json()
        now = time.time()
        self._grants = {k: v for k, v in self._grants.items() if v.expires_at > now}
        grant = Grant(
            code=secrets.token_urlsafe(32),
            scopes=[SCOPE],
            expires_at=now + CODE_TTL,
            client_id=client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            subject=str(tokens["connected_app_id"]),
            api_token=tokens["api_token"],
            refresh_token=tokens["refresh_token"],
        )
        self._grants[grant.code] = grant
        return redirect(code=grant.code)

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> Grant | None:
        return self._grants.get(authorization_code)

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: Grant
    ) -> OAuthToken:
        if self._grants.pop(authorization_code.code, None) is None:
            raise TokenError("invalid_grant")
        return self._issue(
            authorization_code.client_id,
            int(authorization_code.subject or 0),
            authorization_code.api_token,
            authorization_code.refresh_token,
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        # web_app holds refresh tokens; exchange_refresh_token asks it
        return RefreshToken(
            token=refresh_token, client_id=client.client_id, scopes=[SCOPE]
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        response = await self._web_app.post(
            "/internal/mcp/renew",
            json={"refresh_token": refresh_token.token, "client_id": client.client_id},
        )
        if response.status_code == 403:
            raise TokenError("invalid_grant")
        # any other failure is a 500: invalid_grant would make the host drop a good token
        response.raise_for_status()
        renewed = response.json()
        return self._issue(
            client.client_id,
            renewed["connected_app_id"],
            renewed["api_token"],
            renewed["refresh_token"],
        )

    def _issue(
        self, client_id: str, connected_app_id: int, api_token: str, refresh_token: str
    ) -> OAuthToken:
        now = time.time()
        self._tokens = {k: v for k, v in self._tokens.items() if v.expires_at > now}
        access_token = ACCESS_PREFIX + secrets.token_urlsafe(32)
        self._tokens[access_token] = IssuedToken(
            now + ACCESS_TTL, client_id, connected_app_id, api_token
        )
        return OAuthToken(
            access_token=access_token,
            expires_in=ACCESS_TTL,
            scope=SCOPE,
            refresh_token=refresh_token,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        if not token.startswith(ACCESS_PREFIX):
            return await self._accounts.verify_token(token)
        issued = self._tokens.get(token)
        if issued is None or issued.expires_at < time.time():
            return None
        # None once the API key is regenerated: the host renews and gets the new one
        account = await self._accounts.verify_token(issued.api_token)
        if account is None:
            return None
        return AccessToken(
            token=token,
            client_id=issued.client_id,
            scopes=[SCOPE],
            expires_at=int(issued.expires_at),
            resource=self.resource,
            subject=str(issued.connected_app_id),
            claims=account.claims,
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        # no revoke endpoint is served: the dashboard's Revoke is the only path
        raise NotImplementedError
