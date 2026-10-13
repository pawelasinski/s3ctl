from __future__ import annotations

import argparse
import fnmatch
from collections.abc import Iterable
from datetime import UTC
from pathlib import Path, PurePosixPath
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


class ToolError(Exception):
    """Human-readable CLI error."""


def make_session(args: argparse.Namespace) -> boto3.Session:
    """Create a session from CLI overrides and boto3's environment/profile chain.

    Environment variables are exported by the shell or direnv; this function
    does not read .env files. With no explicit credential flags, boto3 reads
    AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY and AWS_SESSION_TOKEN. An explicit
    profile bypasses those environment credentials.

    Parameters
    ----------
    args : argparse.Namespace
        Parsed command-line options.
    """
    explicit_creds = bool(args.access_key or args.secret_key or args.session_token)

    if args.profile and explicit_creds:
        raise ToolError(
            "Укажите либо --profile, либо явные credentials, но не оба варианта сразу."
        )
    if args.access_key and not args.secret_key:
        raise ToolError("Для --access-key нужно также передать --secret-key.")
    if args.secret_key and not args.access_key:
        raise ToolError("Для --secret-key нужно также передать --access-key.")
    if args.session_token and not (args.access_key and args.secret_key):
        raise ToolError(
            "--session-token используется вместе с --access-key и --secret-key."
        )

    kwargs: dict[str, Any] = {}
    if args.profile:
        kwargs["profile_name"] = args.profile
    if args.region:
        kwargs["region_name"] = args.region
    if args.access_key:
        kwargs["aws_access_key_id"] = args.access_key
        kwargs["aws_secret_access_key"] = args.secret_key
        if args.session_token:
            kwargs["aws_session_token"] = args.session_token

    return boto3.Session(**kwargs)


def make_s3_client(args: argparse.Namespace):
    """Create an S3 client with the selected endpoint and addressing style.

    Parameters
    ----------
    args : argparse.Namespace
        Parsed command-line options.
    """
    session = make_session(args)
    return session.client(
        "s3",
        endpoint_url=args.endpoint_url,
        config=Config(s3={"addressing_style": args.addressing_style}),
    )


def make_sts_client(args: argparse.Namespace):
    # endpoint-url is intended for S3-compatible storage; STS usually remains AWS-native.
    """Create an AWS STS client; the S3 endpoint override is not applied.

    Parameters
    ----------
    args : argparse.Namespace
        Parsed command-line options.
    """
    session = make_session(args)
    return session.client("sts")


def human_size(size: int) -> str:
    """Format a byte count using binary multiples of 1024.

    Parameters
    ----------
    size : int
        Size.
    """
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024
    return f"{size} B"


def format_dt(value: Any) -> str:
    """Format timestamps in UTC, retaining naive timestamps as supplied.

    Parameters
    ----------
    value : object
        Value.
    """
    if not value:
        return "-"
    try:
        if value.tzinfo is not None:
            value = value.astimezone(UTC)
            return value.strftime("%Y-%m-%d %H:%M:%S UTC")
        return value.strftime("%Y-%m-%d %H:%M:%S")
    except AttributeError:
        return str(value)


def error_info(exc: ClientError) -> tuple[str, str, int | None, str | None]:
    """Extract the error code, message, HTTP status and bucket region.

    Parameters
    ----------
    exc : botocore.exceptions.ClientError
        AWS API error.
    """
    response = exc.response or {}
    error = response.get("Error", {})
    metadata = response.get("ResponseMetadata", {})
    headers = metadata.get("HTTPHeaders", {}) or {}

    code = str(error.get("Code", "Unknown"))
    message = str(error.get("Message", ""))
    status = metadata.get("HTTPStatusCode")
    region = headers.get("x-amz-bucket-region") or headers.get("X-Amz-Bucket-Region")
    return code, message, status, region


def describe_client_error(exc: ClientError) -> str:
    """Describe an AWS API error without assuming the service is S3.

    Parameters
    ----------
    exc : botocore.exceptions.ClientError
        AWS API error.
    """
    code, message, status, region = error_info(exc)
    region_hint = f" Ожидаемый регион bucket: {region}." if region else ""

    auth_errors = {
        "InvalidAccessKeyId": "Неверный access key ID.",
        "SignatureDoesNotMatch": "Подпись запроса не совпала. Проверьте secret key, регион и endpoint-url.",
        "ExpiredToken": "Session token истёк.",
        "InvalidToken": "Session token некорректен.",
    }
    if code in auth_errors:
        return auth_errors[code]
    if code in {"AccessDenied", "AccessDeniedException", "403"} or status == 403:
        return f"Доступ запрещён для {exc.operation_name} ({code}). Проверьте права и credentials.{region_hint}"
    if code == "NoSuchKey":
        return "Объект с таким key не найден."
    if code == "NoSuchEntity":
        return "Указанный IAM-пользователь или policy не найден."
    if code == "NoSuchBucket":
        return f"Bucket не найден.{region_hint}"
    if code in {"404", "NotFound"} or status == 404:
        return f"Ресурс не найден или его существование скрыто правами доступа.{region_hint}"
    if (
        code
        in {
            "AuthorizationHeaderMalformed",
            "IllegalLocationConstraintException",
            "PermanentRedirect",
            "301",
        }
        or status == 301
    ):
        return f"Похоже, выбран неправильный регион или endpoint-url.{region_hint}"
    details = f"{code}"
    if status:
        details += f" / HTTP {status}"
    if message:
        details += f": {message}"
    return f"Ошибка AWS API ({exc.operation_name}): {details}{region_hint}"


def describe_access_error(exc: ClientError, bucket: str) -> str:
    """Describe a failed S3 probe without claiming the bucket is absent.

    Parameters
    ----------
    exc : botocore.exceptions.ClientError
        AWS API error.
    bucket : str
        Bucket name.
    """
    code, message, status, region = error_info(exc)
    region_hint = f" Регион bucket: {region}." if region else ""

    if code in {
        "InvalidAccessKeyId",
        "SignatureDoesNotMatch",
        "ExpiredToken",
        "InvalidToken",
    }:
        return f"Ошибка авторизации для bucket {bucket}: {describe_client_error(exc)}"
    if (
        code
        in {
            "AuthorizationHeaderMalformed",
            "IllegalLocationConstraintException",
            "PermanentRedirect",
            "301",
        }
        or status == 301
    ):
        return f"Регион не совпадает или endpoint-url неверный для bucket {bucket}.{region_hint}"
    if status == 403 or code in {"AccessDenied", "403"}:
        return f"Доступа к bucket {bucket} нет или операция запрещена.{region_hint}"
    if code == "NoSuchKey":
        return "Объект с таким key не найден; права чтения не подтверждены."
    if status == 404 or code in {"NoSuchBucket", "404", "NotFound"}:
        return f"Bucket {bucket} не найден или его существование скрыто политикой доступа.{region_hint}"

    suffix = f": {message}" if message else ""
    return f"Не удалось проверить доступ к bucket {bucket}: {code}{suffix}{region_hint}"


def normalize_region(location: str | None) -> str:
    """Map legacy S3 location values to AWS region names.

    Parameters
    ----------
    location : str or None
        Location.
    """
    if not location:
        return "us-east-1"
    if location == "EU":
        return "eu-west-1"
    return location


def get_bucket_region(s3, bucket: str) -> str:
    """Determine a bucket region, returning unknown if access is restricted.

    Parameters
    ----------
    s3 : botocore.client.BaseClient
        Configured S3 client.
    bucket : str
        Bucket name.
    """
    try:
        response = s3.head_bucket(Bucket=bucket)
        headers = response.get("ResponseMetadata", {}).get("HTTPHeaders", {}) or {}
        region = headers.get("x-amz-bucket-region") or headers.get(
            "X-Amz-Bucket-Region"
        )
        if region:
            return normalize_region(region)
    except ClientError as exc:
        _, _, _, region = error_info(exc)
        if region:
            return normalize_region(region)

    try:
        response = s3.get_bucket_location(Bucket=bucket)
        return normalize_region(response.get("LocationConstraint"))
    except ClientError as exc:
        code, _, _, _ = error_info(exc)
        return f"unknown ({code})"


def iter_s3_objects(s3, bucket: str, prefix: str = ""):
    """Yield all current S3 objects under a prefix using pagination.

    Parameters
    ----------
    s3 : botocore.client.BaseClient
        Configured S3 client.
    bucket : str
        Bucket name.
    prefix : str
        S3 key prefix.
    """
    paginator = s3.get_paginator("list_objects_v2")
    params: dict[str, Any] = {"Bucket": bucket}
    if prefix:
        params["Prefix"] = prefix

    for page in paginator.paginate(**params):
        yield from page.get("Contents", [])


def iter_s3_listing(s3, bucket: str, prefix: str = "", depth: int | None = 1):
    """Yield objects and grouped prefixes up to the requested depth.

    Parameters
    ----------
    s3 : botocore.client.BaseClient
        S3 client.
    bucket : str
        Bucket name.
    prefix : str
        Literal starting prefix; use a trailing slash for a folder.
    depth : int or None
        Number of levels from the prefix; None lists all objects recursively.
    """
    if depth is None:
        yield from iter_s3_objects(s3, bucket, prefix)
        return
    if depth < 1:
        raise ToolError("--depth must be at least 1")
    # A stack avoids scanning objects below the requested depth.
    pending = [(prefix, 1)]
    paginator = s3.get_paginator("list_objects_v2")
    while pending:
        current, level = pending.pop()
        for page in paginator.paginate(Bucket=bucket, Prefix=current, Delimiter="/"):
            yield from page.get("Contents", [])
            for group in page.get("CommonPrefixes", []):
                child = group["Prefix"]
                yield {"Key": child, "IsPrefix": True}
                if level < depth:
                    pending.append((child, level + 1))


def static_prefix_from_pattern(pattern: str) -> str:
    """Extract the literal prefix before the first glob metacharacter.

    Parameters
    ----------
    pattern : str
        Case-sensitive glob over full S3 keys.
    """
    wildcard_positions = [pattern.find(ch) for ch in "*?[" if pattern.find(ch) != -1]
    first_wildcard = min(wildcard_positions) if wildcard_positions else len(pattern)
    return pattern[:first_wildcard]


def find_objects_by_pattern(s3, bucket: str, pattern: str) -> list[dict[str, Any]]:
    """Find objects whose full keys match a case-sensitive glob.

    Parameters
    ----------
    s3 : botocore.client.BaseClient
        Configured S3 client.
    bucket : str
        Bucket name.
    pattern : str
        Case-sensitive glob over full S3 keys.
    """
    prefix = static_prefix_from_pattern(pattern)
    return [
        obj
        for obj in iter_s3_objects(s3, bucket, prefix)
        if fnmatch.fnmatchcase(obj["Key"], pattern)
    ]


def join_s3_key(*parts: str | None) -> str:
    """Join prefix fragments, trimming only the boundary slashes."""
    cleaned = [part.strip("/") for part in parts if part]
    return "/".join(part for part in cleaned if part)


def safe_local_path(output_dir: str, key: str) -> Path:
    """Resolve an S3 key without escaping the destination directory.

    Parameters
    ----------
    output_dir : str
        Root directory for downloads.
    key : str
        Exact S3 object key.

    Returns
    -------
    pathlib.Path
        Validated destination path.

    Raises
    ------
    ToolError
        If the key is unsafe or escapes the destination.
    """
    base = Path(output_dir).expanduser().resolve()
    parts = key.split("/")
    if (
        not key
        or PurePosixPath(key).is_absolute()
        or any(part in {"", ".", ".."} for part in parts)
        or "\\" in key
        or any(ord(c) < 32 for c in key)
    ):
        raise ToolError(f"Unsafe S3 key for local download: {key!r}")
    target = base.joinpath(*parts)
    resolved = target.resolve()
    if base not in resolved.parents:
        raise ToolError(f"S3 key escapes output directory: {key!r}")
    return target


def unique_keys(keys: Iterable[str]) -> list[str]:
    """Deduplicate keys while preserving their original order.

    Parameters
    ----------
    keys : iterable of str
        Object keys, preserving order.
    """
    return list(dict.fromkeys(keys))


def make_iam_client(args: argparse.Namespace):
    """Create an AWS IAM client using the selected credentials.

    Parameters
    ----------
    args : argparse.Namespace
        Parsed command-line options.
    """
    return make_session(args).client("iam")


def project_dir() -> Path:
    """Find the source project's directory, falling back to the working directory."""
    package = Path(__file__).resolve().parent
    source = package.parent
    directory = source.parent
    if source.name == "src" and (directory / "pyproject.toml").is_file():
        return directory
    return Path.cwd()


def default_output_dir() -> str:
    """Return the project's persistent temporary download directory."""
    return str(project_dir() / "tmp")
