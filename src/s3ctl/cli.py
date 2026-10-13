"""Command-line parsing and application entry point."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from boto3.exceptions import S3UploadFailedError
from botocore.exceptions import BotoCoreError, ClientError, ProfileNotFound
from rich.console import Console

from .core import ToolError, default_output_dir, describe_client_error, project_dir
from .operations import (
    cmd_bucket_policy,
    cmd_check_access,
    cmd_delete,
    cmd_download,
    cmd_download_bucket,
    cmd_list_buckets,
    cmd_list_objects,
    cmd_upload,
    cmd_user_access,
    cmd_user_exists,
    cmd_user_policy,
    cmd_whoami,
)
from .ui import setup_logging

logger = logging.getLogger("s3ctl")


class RichArgumentParser(argparse.ArgumentParser):
    """Display argparse help and usage through Rich without interpreting markup."""

    def _print_message(self, message, file=None):
        if message:
            Console(file=file or sys.stdout, markup=False, highlight=False).print(
                message, end="", soft_wrap=True
            )


def add_auth_args(parser: argparse.ArgumentParser) -> None:
    """Add shared credential, endpoint and logging options.

    Parameters
    ----------
    parser : argparse.ArgumentParser
        Parser receiving the shared command options.
    """
    parser.add_argument(
        "--access-key",
        help="AWS access key ID (otherwise: AWS_ACCESS_KEY_ID or AWS credential chain)",
    )
    parser.add_argument(
        "--secret-key",
        help="AWS secret access key (otherwise: AWS_SECRET_ACCESS_KEY or AWS credential chain)",
    )
    parser.add_argument(
        "--session-token",
        help="Token for explicit temporary keys (otherwise: AWS_SESSION_TOKEN or AWS credential chain)",
    )
    parser.add_argument(
        "--profile",
        help="AWS profile; overrides environment credentials (otherwise: AWS_PROFILE / credential chain)",
    )
    parser.add_argument(
        "--region",
        help="AWS region (otherwise: AWS_DEFAULT_REGION or profile configuration)",
    )
    parser.add_argument(
        "--endpoint-url",
        default=(os.getenv("S3_ENDPOINT_URL") or "").strip() or None,
        help="S3-compatible endpoint (default: S3_ENDPOINT_URL)",
    )
    parser.add_argument(
        "--addressing-style",
        choices=["auto", "path", "virtual"],
        default=(os.getenv("S3_ADDRESSING_STYLE") or "").strip() or "auto",
        help="S3 addressing style (default: auto or S3_ADDRESSING_STYLE); path for some MinIO servers",
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=project_dir() / "logs",
        help="Log directory (default: project/logs)",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Enable application debug logging"
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser and register S3 and IAM command handlers."""
    parser = RichArgumentParser(
        prog="s3ctl",
        description="CLI utility for Amazon S3 / S3-compatible storage operations.",
    )
    default_bucket = (os.getenv("S3_BUCKET") or "").strip() or None
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    auth_parent = argparse.ArgumentParser(add_help=False)
    add_auth_args(auth_parent)

    whoami = subparsers.add_parser(
        "whoami", parents=[auth_parent], help="Show current AWS user/role"
    )
    whoami.set_defaults(func=cmd_whoami)

    check_access = subparsers.add_parser(
        "check-access", parents=[auth_parent], help="Check access to a bucket"
    )
    check_access.add_argument(
        "--bucket",
        default=default_bucket,
        required=default_bucket is None,
        help="Bucket to check (default: S3_BUCKET)",
    )
    check_access.add_argument("--prefix", default="", help="Prefix for live list check")
    check_access.add_argument(
        "--key", help="Also test reading this exact object (first byte)"
    )
    check_access.add_argument(
        "--write-check",
        action="store_true",
        help="Test PUT and DELETE using a unique temporary object under --prefix",
    )
    check_access.set_defaults(func=cmd_check_access)

    ls_buckets = subparsers.add_parser(
        "ls-buckets", parents=[auth_parent], help="List available buckets"
    )
    ls_buckets.set_defaults(func=cmd_list_buckets)

    ls = subparsers.add_parser(
        "ls", parents=[auth_parent], help="List objects in a bucket"
    )
    ls.add_argument(
        "--bucket",
        default=default_bucket,
        required=default_bucket is None,
        help="Bucket name (default: S3_BUCKET)",
    )
    ls.add_argument("--prefix", default="", help="Key prefix filter")
    levels = ls.add_mutually_exclusive_group()
    levels.add_argument(
        "--recursive", action="store_true", help="List objects at all levels"
    )
    levels.add_argument(
        "--depth", type=int, help="Levels relative to bucket or prefix (default: 1)"
    )
    ls.add_argument(
        "--sort-by",
        choices=["name", "last_modified", "size"],
        default="name",
        help="Sort field",
    )
    ls.add_argument(
        "--order", choices=["asc", "desc"], default="asc", help="Sort order"
    )
    ls.add_argument(
        "--pager",
        action="store_true",
        help="Scroll the full listing in a terminal pager",
    )
    ls.set_defaults(func=cmd_list_objects)

    get_bucket = subparsers.add_parser(
        "get-bucket", parents=[auth_parent], help="Download entire bucket or prefix"
    )
    get_bucket.add_argument(
        "--bucket",
        default=default_bucket,
        required=default_bucket is None,
        help="Bucket name (default: S3_BUCKET)",
    )
    get_bucket.add_argument("--prefix", default="", help="Key prefix to download")
    get_bucket.add_argument(
        "--output-dir",
        default=default_output_dir(),
        help="Output directory (default: project/tmp; existing files are replaced)",
    )
    get_bucket.set_defaults(func=cmd_download_bucket)

    get = subparsers.add_parser(
        "get", parents=[auth_parent], help="Download one or more objects"
    )
    get.add_argument(
        "--bucket",
        default=default_bucket,
        required=default_bucket is None,
        help="Bucket name (default: S3_BUCKET)",
    )
    get_selector = get.add_mutually_exclusive_group(required=True)
    get_selector.add_argument("--key", nargs="+", help="One or more exact S3 keys")
    get_selector.add_argument(
        "--pattern", help="fnmatch pattern, e.g., '*.csv' or 'logs/2025-*'"
    )
    get.add_argument(
        "--output-dir",
        default=default_output_dir(),
        help="Output directory (default: project/tmp; existing files are replaced)",
    )
    get.set_defaults(func=cmd_download)

    put = subparsers.add_parser(
        "put", parents=[auth_parent], help="Upload a file or directory"
    )
    put.add_argument(
        "--bucket",
        default=default_bucket,
        required=default_bucket is None,
        help="Target bucket (default: S3_BUCKET)",
    )
    put.add_argument("--local-path", required=True, help="Local file or directory path")
    put.add_argument(
        "--s3-key",
        help="Exact file key (prefix prepended), or base prefix for a directory",
    )
    put.add_argument("--prefix", default="", help="Base prefix for uploads")
    put.set_defaults(func=cmd_upload)

    delete = subparsers.add_parser(
        "delete", parents=[auth_parent], help="Delete objects safely"
    )
    delete.add_argument(
        "--bucket",
        default=default_bucket,
        required=default_bucket is None,
        help="Bucket name (default: S3_BUCKET)",
    )
    delete_selector = delete.add_mutually_exclusive_group(required=True)
    delete_selector.add_argument("--key", nargs="+", help="One or more exact S3 keys")
    delete_selector.add_argument("--pattern", help="fnmatch pattern, e.g., 'tmp/*'")
    delete.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be deleted without deleting",
    )
    delete.add_argument(
        "--yes", action="store_true", help="Explicitly confirm actual deletion"
    )
    delete.set_defaults(func=cmd_delete)

    exists = subparsers.add_parser(
        "user-exists",
        parents=[auth_parent],
        help="Check whether an AWS IAM user exists",
    )
    exists.add_argument("--user-name", required=True, help="AWS IAM user name")
    exists.set_defaults(func=cmd_user_exists)

    policy = subparsers.add_parser(
        "user-policy",
        parents=[auth_parent],
        help="Show AWS IAM user, group and boundary policies",
    )
    policy.add_argument("--user-name", required=True, help="AWS IAM user name")
    policy.set_defaults(func=cmd_user_policy)

    bucket_policy = subparsers.add_parser(
        "bucket-policy", parents=[auth_parent], help="Show bucket policy"
    )
    bucket_policy.add_argument(
        "--bucket",
        default=default_bucket,
        required=default_bucket is None,
        help="Bucket name (default: S3_BUCKET)",
    )
    bucket_policy.set_defaults(func=cmd_bucket_policy)

    access = subparsers.add_parser(
        "user-access",
        parents=[auth_parent],
        help="Simulate AWS IAM user permissions (not live access)",
    )
    access.add_argument("--user-name", required=True, help="AWS IAM user name")
    access.add_argument(
        "--bucket",
        default=default_bucket,
        required=default_bucket is None,
        help="Bucket name (default: S3_BUCKET)",
    )
    access.add_argument(
        "--key",
        help="Object key for IAM simulation; * and ? are wildcards (default: bucket/*)",
    )
    access.set_defaults(func=cmd_user_access)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, initialize logs and execute one CLI command.

    Parameters
    ----------
    argv : list of str or None, optional
        Arguments excluding the executable name; None uses sys.argv.

    Returns
    -------
    int
        Zero on success, one on error, two on failed checks, 130 on interruption.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        setup_logging(args.log_dir.expanduser(), args.verbose)
    except OSError as exc:
        Console(stderr=True, markup=False).print(
            f"Cannot initialize file logging: {exc}"
        )
        return 1
    try:
        logger.info("Command: %s", args.command)
        return int(args.func(args) or 0)
    except ToolError as exc:
        logger.error("%s", exc)
    except ProfileNotFound:
        logger.error("AWS profile not found; check --profile or AWS_PROFILE")
    except ClientError as exc:
        logger.error("%s", describe_client_error(exc))
    except BotoCoreError as exc:
        logger.error("AWS client error: %s", exc)
    except (OSError, ValueError, S3UploadFailedError) as exc:
        logger.error("%s", exc)
    except KeyboardInterrupt:
        logger.warning("Interrupted")
        return 130
    return 1
