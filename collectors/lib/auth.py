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


def boto3_session(profile: str | None = None, region: str | None = None):
    """Return a boto3 Session for the given profile and region.

    Boto3 is imported lazily so non-AWS collectors don't pay the import cost
    and don't need boto3 installed.
    """
    import boto3

    return boto3.Session(profile_name=profile or None, region_name=region or None)


def es_api_key_headers(encoded_key: str) -> dict[str, str]:
    """Headers for Elasticsearch API key auth.

    Expects the already-base64-encoded key string (as returned by the
    Create API Key response's ``encoded`` field).
    """
    return {"Authorization": f"ApiKey {encoded_key}"}


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
