"""Per-platform OAuth2 configuration.

One dataclass per platform so the flow code stays provider-agnostic. Anything
platform-specific — PKCE, how the client authenticates at the token endpoint,
the mandatory Reddit User-Agent — is data here, not a branch in the flow.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class OAuthProvider:
    name: str
    label: str
    authorize_url: str
    token_url: str
    identity_url: str
    client_id_env: str
    client_secret_env: str
    default_scopes: tuple[str, ...]
    #: Some providers want space-delimited scopes, Reddit wants commas.
    scope_separator: str = " "
    #: X mandates PKCE; LinkedIn and Reddit do not use it.
    use_pkce: bool = False
    #: "basic" puts client_id/secret in an Authorization header at /token.
    token_auth: str = "body"
    extra_authorize_params: Mapping[str, str] = field(default_factory=dict)
    #: Reddit rejects requests without a descriptive User-Agent.
    user_agent: str | None = None
    max_chars: int = 3000
    daily_cap_default: int = 3
    #: False where the platform forbids or blocks programmatic replies.
    allows_replies: bool = False
    #: Shown in the console when the platform cannot be enabled.
    caveat: str = ""

    def client_id(self) -> str:
        return os.environ.get(self.client_id_env, "").strip()

    def client_secret(self) -> str:
        return os.environ.get(self.client_secret_env, "").strip()

    def configured(self) -> bool:
        return bool(self.client_id() and self.client_secret())

    def scope_value(self, scopes: tuple[str, ...] | None = None) -> str:
        return self.scope_separator.join(scopes or self.default_scopes)


LINKEDIN = OAuthProvider(
    name="linkedin",
    label="LinkedIn",
    authorize_url="https://www.linkedin.com/oauth/v2/authorization",
    token_url="https://www.linkedin.com/oauth/v2/accessToken",
    identity_url="https://api.linkedin.com/v2/userinfo",
    client_id_env="LINKEDIN_CLIENT_ID",
    client_secret_env="LINKEDIN_CLIENT_SECRET",
    # "Share on LinkedIn" is a self-serve product in the developer portal and
    # is what grants w_member_social. Company pages need w_organization_social,
    # which requires the reviewed Community Management API.
    default_scopes=("openid", "profile", "w_member_social"),
    max_chars=3000,
    daily_cap_default=1,
    allows_replies=False,
    caveat=(
        "User Agreement 8.2 prohibits bot-driven commenting, so this posts only."
    ),
)

REDDIT = OAuthProvider(
    name="reddit",
    label="Reddit",
    authorize_url="https://www.reddit.com/api/v1/authorize",
    token_url="https://www.reddit.com/api/v1/access_token",
    identity_url="https://oauth.reddit.com/api/v1/me",
    client_id_env="REDDIT_CLIENT_ID",
    client_secret_env="REDDIT_CLIENT_SECRET",
    default_scopes=("identity", "submit", "read", "edit", "mysubreddits", "flair"),
    scope_separator=",",
    token_auth="basic",
    # duration=permanent is what makes Reddit issue a refresh token at all.
    extra_authorize_params={"duration": "permanent"},
    user_agent=os.environ.get(
        "REDDIT_USER_AGENT", "companyos-content/1.0 (by /u/companyos)"
    ),
    max_chars=40000,
    daily_cap_default=1,
    allows_replies=False,
    caveat=(
        "Self-serve app creation closed in November 2025. Free-tier access is "
        "non-commercial; a company needs Reddit's written commercial approval. "
        "Automated comments are treated as spam under the Responsible Builder Policy."
    ),
)

X = OAuthProvider(
    name="x",
    label="X",
    authorize_url="https://x.com/i/oauth2/authorize",
    token_url="https://api.x.com/2/oauth2/token",
    identity_url="https://api.x.com/2/users/me",
    client_id_env="X_CLIENT_ID",
    client_secret_env="X_CLIENT_SECRET",
    default_scopes=("tweet.read", "tweet.write", "users.read", "offline.access"),
    use_pkce=True,
    token_auth="basic",
    max_chars=280,
    daily_cap_default=3,
    allows_replies=False,
    caveat=(
        "Replies return 403 on self-serve tiers since February 2026 unless the "
        "author summoned you. Posts containing a URL cost 13x more."
    ),
)


PROVIDERS: dict[str, OAuthProvider] = {p.name: p for p in (LINKEDIN, REDDIT, X)}


def get_provider(platform: str) -> OAuthProvider:
    key = (platform or "").strip().lower()
    try:
        return PROVIDERS[key]
    except KeyError as exc:
        raise KeyError(f"unknown publishing platform: {platform}") from exc


def configured_platforms() -> list[str]:
    return sorted(name for name, p in PROVIDERS.items() if p.configured())


def provider_status() -> list[dict[str, object]]:
    """What the console shows on the Content page for each platform."""
    overlays: dict[str, dict[str, object]] = {}
    try:
        from app.content import composio_social

        overlays = composio_social.provider_overlays()  # type: ignore[assignment]
    except Exception:
        overlays = {}

    out: list[dict[str, object]] = []
    for p in PROVIDERS.values():
        overlay = overlays.get(p.name) or {}
        composio_ready = bool(overlay.get("composio_managed") or overlay.get("connectable"))
        from app.admin.authn import require_signed_caller
        native_configured = p.configured()
        native_ready = native_configured and (not require_signed_caller() or p.use_pkce)
        connectable = bool(overlay.get("connectable")) or native_ready
        caveat = str(overlay.get("composio_caveat") or "") or p.caveat
        if native_configured and not native_ready and not composio_ready:
            caveat = "Hosted authorization requires a managed Composio connection for this provider."
        out.append(
            {
                "platform": p.name,
                "label": p.label,
                # "configured" now means "Connect is available" (Composio or native).
                "configured": connectable,
                "native_configured": native_configured,
                "composio": bool(overlay.get("composio")),
                "composio_managed": bool(overlay.get("composio_managed")),
                "auth_via_default": overlay.get("auth_via_default") or (
                    "native" if native_ready else "none"
                ),
                "client_id_env": p.client_id_env,
                "client_secret_env": p.client_secret_env,
                "scopes": list(p.default_scopes),
                "max_chars": p.max_chars,
                "allows_replies": p.allows_replies,
                "caveat": caveat,
                "composio_ready": composio_ready and not native_ready,
            }
        )
    return out
