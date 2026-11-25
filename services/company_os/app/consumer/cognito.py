"""Cognito helpers for the existing production user pool.

Pool ``us-east-1_NZoV9G3NP``, SPA client ``4g1g49rph4fugqfo31npjmbo50``.
Tests inject ``FakeCognitoClient`` so nothing in CI talks to AWS.
"""

from __future__ import annotations

from core.deployment import infrastructure
import json
import os
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

_LOCAL_MODE = os.environ.get("SUPYMEM_MODE", "").strip().lower() == "local"
PUBLIC_POOL_ID = "" if _LOCAL_MODE else (os.environ.get("COGNITO_USER_POOL_ID") or infrastructure('${COGNITO_POOL_ID}'))
PUBLIC_CLIENT_ID = "" if _LOCAL_MODE else (os.environ.get("COGNITO_CLIENT_ID") or infrastructure('${COGNITO_CLIENT_ID}'))
PUBLIC_REGION = "us-east-1"
PUBLIC_DOMAIN = "" if _LOCAL_MODE else (os.environ.get("COGNITO_DOMAIN") or infrastructure('https://${LEGACY_PRODUCTION_PREFIX}-auth.auth.us-east-1.amazoncognito.com'))


class CognitoError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class CognitoIdentity:
    sub: str
    email: str
    email_verified: bool = True


class CognitoClient(Protocol):
    def start_email_otp(self, email: str) -> dict[str, Any]: ...

    def verify_email_otp(self, email: str, code: str) -> CognitoIdentity: ...

    def exchange_oauth_code(
        self,
        code: str,
        redirect_uri: str,
        code_verifier: str | None = None,
    ) -> CognitoIdentity: ...

    def verify_id_token(self, token: str) -> CognitoIdentity: ...


def pool_id() -> str:
    return (os.environ.get("COGNITO_USER_POOL_ID") or PUBLIC_POOL_ID or infrastructure('${COGNITO_POOL_ID}')).strip()


def client_id() -> str:
    return (os.environ.get("COGNITO_CLIENT_ID") or PUBLIC_CLIENT_ID or infrastructure('${COGNITO_CLIENT_ID}')).strip()


def region() -> str:
    return (os.environ.get("COGNITO_REGION") or PUBLIC_REGION).strip()


def hosted_domain() -> str:
    return (
        os.environ.get("COGNITO_DOMAIN") or PUBLIC_DOMAIN
        or infrastructure('https://${LEGACY_PRODUCTION_PREFIX}-auth.auth.us-east-1.amazoncognito.com')
    ).strip().rstrip("/")


def _norm_email(email: str) -> str:
    value = (email or "").strip().lower()
    if "@" not in value:
        raise CognitoError("invalid_email", "A valid email is required")
    return value


class FakeCognitoClient:
    """Deterministic in-process Cognito. No network."""

    def __init__(self) -> None:
        self.otp: dict[str, str] = {}
        self.oauth_codes: dict[str, CognitoIdentity] = {}
        self.id_tokens: dict[str, CognitoIdentity] = {}
        self.default_otp = "246801"

    def start_email_otp(self, email: str) -> dict[str, Any]:
        addr = _norm_email(email)
        self.otp[addr] = self.default_otp
        return {"ok": True, "challenge": "EMAIL_OTP"}

    def verify_email_otp(self, email: str, code: str) -> CognitoIdentity:
        addr = _norm_email(email)
        expected = self.otp.get(addr)
        if not expected or (code or "").strip() != expected:
            raise CognitoError("invalid_code", "That code was not accepted")
        ident = CognitoIdentity(sub=f"fake-{addr}", email=addr)
        self.id_tokens[f"id:{addr}"] = ident
        return ident

    def exchange_oauth_code(
        self,
        code: str,
        redirect_uri: str,
        code_verifier: str | None = None,
    ) -> CognitoIdentity:
        _ = (redirect_uri, code_verifier)
        ident = self.oauth_codes.get((code or "").strip())
        if ident is None:
            raise CognitoError("invalid_grant", "Authorization code was not accepted")
        return ident

    def verify_id_token(self, token: str) -> CognitoIdentity:
        ident = self.id_tokens.get((token or "").strip())
        if ident is None:
            raise CognitoError("invalid_token", "ID token was not accepted")
        return ident

    def register_oauth(self, code: str, identity: CognitoIdentity) -> None:
        self.oauth_codes[code] = identity


class HttpCognitoClient:
    """Public Cognito HTTP (no new AWS resources; uses the existing pool)."""

    def __init__(self, *, http_client: httpx.Client | None = None) -> None:
        self._http = http_client
        self._otp_sessions: dict[str, str] = {}
        self._jwks: dict[str, Any] | None = None

    def _client(self) -> httpx.Client:
        return self._http or httpx.Client(timeout=20.0)

    def start_email_otp(self, email: str) -> dict[str, Any]:
        addr = _norm_email(email)
        body = {
            "AuthFlow": "USER_AUTH",
            "ClientId": client_id(),
            "AuthParameters": {
                "USERNAME": addr,
                "PREFERRED_CHALLENGE": "EMAIL_OTP",
            },
        }
        data = self._cognito_json("AWSCognitoIdentityProviderService.InitiateAuth", body)
        session = data.get("Session")
        if session:
            # Stash session on the instance so verify can complete the challenge.
            self._otp_sessions[addr] = str(session)
        return {"ok": True, "challenge": "EMAIL_OTP"}

    def verify_email_otp(self, email: str, code: str) -> CognitoIdentity:
        addr = _norm_email(email)
        session = self._otp_sessions.get(addr)
        body = {
            "ClientId": client_id(),
            "ChallengeName": "EMAIL_OTP",
            "Session": session or "",
            "ChallengeResponses": {
                "USERNAME": addr,
                "EMAIL_OTP_CODE": (code or "").strip(),
            },
        }
        data = self._cognito_json(
            "AWSCognitoIdentityProviderService.RespondToAuthChallenge", body
        )
        id_token = ((data.get("AuthenticationResult") or {}).get("IdToken")) or ""
        if not id_token:
            raise CognitoError("invalid_code", "That code was not accepted")
        return self.verify_id_token(str(id_token))

    def exchange_oauth_code(
        self,
        code: str,
        redirect_uri: str,
        code_verifier: str | None = None,
    ) -> CognitoIdentity:
        payload = {
            "grant_type": "authorization_code",
            "client_id": client_id(),
            "code": (code or "").strip(),
            "redirect_uri": redirect_uri,
        }
        if code_verifier:
            payload["code_verifier"] = code_verifier
        client = self._client()
        try:
            resp = client.post(
                f"{hosted_domain()}/oauth2/token",
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        finally:
            if self._http is None:
                client.close()
        if resp.status_code >= 400:
            raise CognitoError("invalid_grant", "Authorization code was not accepted")
        body = resp.json()
        id_token = body.get("id_token") or body.get("IdToken")
        if not id_token:
            raise CognitoError("invalid_grant", "Cognito did not return an ID token")
        return self.verify_id_token(str(id_token))

    def verify_id_token(self, token: str) -> CognitoIdentity:
        payload = self._verify_cognito_jwt(token)
        iss = f"https://cognito-idp.{region()}.amazonaws.com/{pool_id()}"
        if payload.get("iss") != iss:
            raise CognitoError("invalid_token", "ID token issuer does not match this pool")
        aud = payload.get("aud")
        if aud != client_id() and payload.get("client_id") != client_id():
            raise CognitoError("invalid_token", "ID token audience does not match this app")
        sub = str(payload.get("sub") or "").strip()
        email = str(payload.get("email") or "").strip().lower()
        if not sub or not email:
            raise CognitoError("invalid_token", "ID token is missing sub or email")
        return CognitoIdentity(
            sub=sub,
            email=email,
            email_verified=bool(payload.get("email_verified", True)),
        )

    def _jwks_json(self) -> dict[str, Any]:
        if self._jwks is not None:
            return self._jwks
        url = (
            f"https://cognito-idp.{region()}.amazonaws.com/{pool_id()}"
            "/.well-known/jwks.json"
        )
        client = self._client()
        try:
            resp = client.get(url)
        finally:
            if self._http is None:
                client.close()
        if resp.status_code >= 400:
            raise CognitoError("invalid_token", "Could not load Cognito signing keys")
        data = resp.json()
        if not isinstance(data, dict) or not data.get("keys"):
            raise CognitoError("invalid_token", "Cognito JWKS was empty")
        self._jwks = data
        return data

    def _verify_cognito_jwt(self, token: str) -> dict[str, Any]:
        import time

        from cryptography.hazmat.primitives.asymmetric import padding, rsa
        from cryptography.hazmat.primitives import hashes

        parts = (token or "").split(".")
        if len(parts) != 3:
            raise CognitoError("invalid_token", "ID token is not a JWT")
        try:
            header = json.loads(_b64url_json(parts[0]))
        except Exception as exc:
            raise CognitoError("invalid_token", "ID token header is not JSON") from exc
        if header.get("alg") != "RS256":
            raise CognitoError("invalid_token", "ID token algorithm is not RS256")
        kid = str(header.get("kid") or "").strip()
        if not kid:
            raise CognitoError("invalid_token", "ID token is missing kid")
        jwk = next(
            (key for key in self._jwks_json().get("keys") or [] if key.get("kid") == kid),
            None,
        )
        if not isinstance(jwk, dict):
            raise CognitoError("invalid_token", "ID token kid is not in Cognito JWKS")
        try:
            n = int.from_bytes(_b64url_bytes(str(jwk["n"])), "big")
            e = int.from_bytes(_b64url_bytes(str(jwk["e"])), "big")
            public = rsa.RSAPublicNumbers(e, n).public_key()
            public.verify(
                _b64url_bytes(parts[2]),
                f"{parts[0]}.{parts[1]}".encode("ascii"),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        except CognitoError:
            raise
        except Exception as exc:
            raise CognitoError("invalid_token", "ID token signature was not accepted") from exc
        payload = _decode_jwt_payload(token)
        if int(payload.get("exp") or 0) < int(time.time()):
            raise CognitoError("invalid_token", "ID token expired")
        return payload

    def _cognito_json(self, target: str, body: dict[str, Any]) -> dict[str, Any]:
        client = self._client()
        try:
            resp = client.post(
                f"https://cognito-idp.{region()}.amazonaws.com/",
                content=json.dumps(body),
                headers={
                    "Content-Type": "application/x-amz-json-1.1",
                    "X-Amz-Target": target,
                },
            )
        finally:
            if self._http is None:
                client.close()
        if resp.status_code >= 400:
            raise CognitoError("cognito_error", "Cognito rejected the request")
        data = resp.json()
        if not isinstance(data, dict):
            raise CognitoError("cognito_error", "Cognito returned an unexpected body")
        return data


def _b64url_bytes(text: str) -> bytes:
    import base64

    padded = text + "=" * ((4 - len(text) % 4) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _b64url_json(text: str) -> str:
    return _b64url_bytes(text).decode("utf-8")


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    parts = (token or "").split(".")
    if len(parts) != 3:
        raise CognitoError("invalid_token", "ID token is not a JWT")
    try:
        payload = json.loads(_b64url_json(parts[1]))
    except Exception as exc:
        raise CognitoError("invalid_token", "ID token payload is not JSON") from exc
    if not isinstance(payload, dict):
        raise CognitoError("invalid_token", "ID token payload is not an object")
    return payload


_client: CognitoClient | None = None


def get_cognito_client() -> CognitoClient:
    global _client
    if _client is not None:
        return _client
    mode = (os.environ.get("COGNITO_CLIENT") or "").strip().lower()
    if mode in {"fake", "stub", "test"}:
        _client = FakeCognitoClient()
        return _client
    return HttpCognitoClient()


def set_cognito_client(client: CognitoClient | None) -> None:
    global _client
    _client = client
