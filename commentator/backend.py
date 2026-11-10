"""Where the app's AWS clients come from: the S3 bucket for the stats page, live video and backups,
and the chat's DynamoDB table.

Every boto3 client is made here so another source of credentials (say, short-lived ones scoped to a
prefix) can be swapped in at one place with `use()`. By default it's what it always was: boto3's own
credential chain (the .env keys, a profile or the instance role)."""
from __future__ import annotations

from typing import Any, Callable

ClientFactory = Callable[[str, "str | None"], Any]


def _boto3_client(service: str, region: str | None = None) -> Any:
    import boto3

    return boto3.client(service, region_name=region) if region else boto3.client(service)


_factory: ClientFactory = _boto3_client


def client(service: str, region: str | None = None) -> Any:
    """A boto3 client for `service` ("s3", "dynamodb"), in `region` if given."""
    return _factory(service, region)


def s3(region: str | None = None) -> Any:
    return client("s3", region)


def dynamodb(region: str | None = None) -> Any:
    return client("dynamodb", region)


def use(factory: ClientFactory | None) -> None:
    """Make clients with `factory(service, region)` from now on; None goes back to boto3's own."""
    global _factory
    _factory = factory or _boto3_client
