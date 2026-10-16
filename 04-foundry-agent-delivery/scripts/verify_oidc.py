"""Fail closed unless the GitHub-issued OIDC token has the expected subject."""

import base64
import json
import os
import sys
import time
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl
from urllib.request import Request, urlopen


class OidcError(RuntimeError):
    pass


def verify_token(request_url, request_token, expected_subject):
    parts = urlsplit(request_url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["audience"] = "api://AzureADTokenExchange"
    url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
    request = Request(url, headers={"Authorization": f"Bearer {request_token}"})
    with urlopen(request, timeout=30) as response:
        token_response = json.load(response)
    token = token_response.get("value")
    if not isinstance(token, str):
        raise OidcError("GitHub OIDC endpoint returned no token.")
    try:
        payload_segment = token.split(".")[1]
        payload = json.loads(
            base64.urlsafe_b64decode(payload_segment + "=" * (-len(payload_segment) % 4))
        )
    except (IndexError, ValueError, json.JSONDecodeError) as error:
        raise OidcError("GitHub OIDC token payload could not be decoded.") from error
    audience = payload.get("aud")
    audiences = audience if isinstance(audience, list) else [audience]
    if (
        payload.get("iss") != "https://token.actions.githubusercontent.com"
        or "api://AzureADTokenExchange" not in audiences
        or payload.get("sub") != expected_subject
        or type(payload.get("exp")) not in (int, float)
        or payload["exp"] <= time.time()
    ):
        actual_subject = payload.get("sub", "<missing>")
        raise OidcError(
            f"OIDC token claims did not match issuer, audience, expected subject "
            f"{expected_subject!r}, and unexpired-token requirements; actual subject "
            f"was {actual_subject!r}."
        )
    return payload["sub"]


def main():
    try:
        subject = verify_token(
            os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"],
            os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"],
            os.environ["EXPECTED_OIDC_SUBJECT"],
        )
        print(f"Verified GitHub OIDC subject: {subject}")
        return 0
    except (OidcError, KeyError, OSError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
