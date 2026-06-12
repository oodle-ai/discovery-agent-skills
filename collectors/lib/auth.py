"""Auth helpers for collectors.

Covered today: basic auth, bearer tokens, vendor API-key headers.
SigV4 (boto3) and gcloud-token auth are added with the opensearch/cloudwatch
and gcp collectors respectively; both import their SDKs lazily so collectors
that don't need them carry no extra dependencies.
"""

from __future__ import annotations

import subprocess


def basic_auth(username: str, password: str) -> tuple[str, str]:
    return (username, password)


def bearer_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def datadog_headers(api_key: str, app_key: str) -> dict[str, str]:
    return {
        "DD-API-KEY": api_key,
        "DD-APPLICATION-KEY": app_key,
        "Accept": "application/json",
    }


def gcloud_access_token() -> str:
    """Fetch an access token from the user's gcloud CLI session."""
    out = subprocess.run(
        ["gcloud", "auth", "print-access-token"],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return out.stdout.strip()
