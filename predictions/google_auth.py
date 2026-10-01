"""Sign in with Google (OpenID Connect authorization-code flow).

Deliberately small and dependency-free (plain `requests`): the views in
views.py send the visitor to build_auth_url(), Google sends them back to the
callback with a one-time code, and exchange_code() swaps that code for the
user's verified Google identity. Enabled only when GOOGLE_CLIENT_ID and
GOOGLE_CLIENT_SECRET are set.
"""

import base64
import json
import re
import time
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.contrib.auth.models import User
from django.urls import reverse

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
ISSUERS = ("accounts.google.com", "https://accounts.google.com")
TIMEOUT = 10

USERNAME_MAX_LENGTH = 30
FALLBACK_USERNAME = "Player"


class GoogleAuthError(Exception):
    """Google sign-in failed; the message is safe to log, not to show."""


def is_enabled():
    return bool(settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET)


def redirect_uri(request):
    return request.build_absolute_uri(reverse("google_callback"))


def build_auth_url(request, state):
    """Google's consent-screen URL; `state` is echoed back to the callback."""
    params = {
        "client_id": settings.GOOGLE_CLIENT_ID,
        "redirect_uri": redirect_uri(request),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        # Always show the account chooser, so someone signed in to several
        # Google accounts picks the right one.
        "prompt": "select_account",
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def exchange_code(request, code):
    """Swap the callback's `code` for the user's Google identity.

    Returns a dict with sub, email (lower-cased, verified by Google), name.
    The ID token comes straight from Google's token endpoint over TLS, so
    per OpenID Connect Core 3.1.3.7 its signature need not be checked; its
    audience, issuer, expiry and email_verified claims still are.
    """
    try:
        response = requests.post(
            TOKEN_URL,
            data={
                "code": code,
                "client_id": settings.GOOGLE_CLIENT_ID,
                "client_secret": settings.GOOGLE_CLIENT_SECRET,
                "redirect_uri": redirect_uri(request),
                "grant_type": "authorization_code",
            },
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        raise GoogleAuthError(f"token request failed: {exc}") from exc
    if response.status_code != 200:
        raise GoogleAuthError(
            f"token endpoint returned {response.status_code}: {response.text[:200]}"
        )
    try:
        claims = _decode_jwt_payload(response.json()["id_token"])
    except (ValueError, KeyError, TypeError) as exc:
        raise GoogleAuthError(f"bad token response: {exc}") from exc

    if claims.get("aud") != settings.GOOGLE_CLIENT_ID:
        raise GoogleAuthError("ID token audience mismatch")
    if claims.get("iss") not in ISSUERS:
        raise GoogleAuthError("ID token issuer mismatch")
    if not isinstance(claims.get("exp"), (int, float)) or claims["exp"] < time.time():
        raise GoogleAuthError("ID token expired")
    if not claims.get("sub") or not claims.get("email"):
        raise GoogleAuthError("ID token lacks sub/email")
    # Google sends a bool; older tokens sent the string "true".
    if claims.get("email_verified") not in (True, "true"):
        raise GoogleAuthError("Google email not verified")

    return {
        "sub": str(claims["sub"]),
        "email": claims["email"].strip().lower(),
        "name": claims.get("name") or claims.get("given_name") or "",
    }


def _decode_jwt_payload(token):
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("not a JWT")
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    claims = json.loads(base64.urlsafe_b64decode(payload))
    if not isinstance(claims, dict):
        raise ValueError("JWT payload is not an object")
    return claims


def generate_username(name):
    """A free username built from a Google display name.

    "Arun Kumar" -> "ArunKumar", or "ArunKumar2", "ArunKumar3", ... if taken
    (compared case-insensitively). Never derived from the email address, as
    usernames are public on the leaderboard.
    """
    base = re.sub(r"[^\w.@+-]", "", name or "")[:USERNAME_MAX_LENGTH]
    base = base or FALLBACK_USERNAME
    candidate, n = base, 1
    while User.objects.filter(username__iexact=candidate).exists():
        n += 1
        suffix = str(n)
        candidate = base[: USERNAME_MAX_LENGTH - len(suffix)] + suffix
    return candidate
