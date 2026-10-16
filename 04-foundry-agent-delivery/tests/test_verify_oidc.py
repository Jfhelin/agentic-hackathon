import base64
import io
import json
import time
import unittest
from unittest.mock import patch

from scripts import verify_oidc


class VerifyOidcTests(unittest.TestCase):
    def test_github_oidc_request_uses_the_request_bearer_token(self):
        claims = {
            "iss": "https://token.actions.githubusercontent.com",
            "aud": "api://AzureADTokenExchange",
            "sub": "repo:workshop/participant:ref:refs/heads/main",
            "exp": int(time.time()) + 300,
        }
        encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        token = f"header.{encoded}.signature"
        response = io.BytesIO(json.dumps({"value": token}).encode())
        with patch("scripts.verify_oidc.urlopen", return_value=response) as urlopen:
            subject = verify_oidc.verify_token(
                "https://token.actions.githubusercontent.com/?foo=bar",
                "github-request-token",
                claims["sub"],
            )
        self.assertEqual(subject, claims["sub"])
        self.assertEqual(
            urlopen.call_args.args[0].get_header("Authorization"),
            "Bearer github-request-token",
        )


if __name__ == "__main__":
    unittest.main()
