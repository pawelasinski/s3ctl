from __future__ import annotations

import argparse
import json
import logging
import tempfile
from pathlib import Path
from uuid import uuid4

from botocore.exceptions import BotoCoreError, ClientError
from rich.progress import track

from .core import (
    ToolError,
    describe_access_error,
    find_objects_by_pattern,
    format_dt,
    get_bucket_region,
    human_size,
    iter_s3_listing,
    iter_s3_objects,
    join_s3_key,
    make_iam_client,
    make_s3_client,
    make_sts_client,
    safe_local_path,
    unique_keys,
)
from .ui import console, print_table, show_json

logger = logging.getLogger("s3ctl")


def download_one(
    s3,
    bucket: str,
    key: str,
    output_dir: str,
    size: int | None = None,
    destinations: dict[tuple[int, int], str] | None = None,
) -> bool:
    """Download an object atomically, skipping only zero-byte folder markers.

    Parameters
    ----------
    s3 : botocore.client.BaseClient
        Configured S3 client.
    bucket : str
        Bucket name.
    key : str
        Exact object key.
    output_dir : str
        Root download directory.
    size : int or None, optional
        Listed object size; a trailing-slash key is checked with HeadObject if unknown.
    destinations : dict or None, optional
        Local file identities mapped to keys downloaded in this command.

    Returns
    -------
    bool
        True after a successful download, False for a zero-byte folder marker.
    """
    if key.endswith("/"):
        if size is None:
            size = s3.head_object(Bucket=bucket, Key=key)["ContentLength"]
        if size != 0:
            raise ToolError(
                f"Cannot map a nonempty trailing-slash key to a file: {key!r}"
            )
        logger.info("Skipped zero-byte folder marker: s3://%s/%s", bucket, key)
        return False

    target = safe_local_path(output_dir, key)
    target.parent.mkdir(parents=True, exist_ok=True)
    if destinations is not None and target.exists():
        info = target.stat()
        previous = destinations.get((info.st_dev, info.st_ino))
        if previous is not None and previous != key:
            raise ToolError(
                f"Local filename collision between S3 keys {previous!r} and {key!r}"
            )
    # Keep existing files intact if a download fails halfway through.
    with tempfile.NamedTemporaryFile(
        dir=target.parent, prefix=".s3ctl-", delete=False
    ) as stream:
        temporary = Path(stream.name)
    try:
        s3.download_file(bucket, key, str(temporary))
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    if destinations is not None:
        info = target.stat()
        destinations[(info.st_dev, info.st_ino)] = key
    logger.info("Downloaded: s3://%s/%s -> %s", bucket, key, target)
    return True


def upload_one(s3, bucket: str, local_path: Path, key: str) -> None:
    """Upload one file and log success only after the transfer completes.

    Parameters
    ----------
    s3 : botocore.client.BaseClient
        Configured S3 client.
    bucket : str
        Bucket name.
    local_path : pathlib.Path
        Local path.
    key : str
        Exact object key.
    """
    s3.upload_file(str(local_path), bucket, key)
    logger.info("Uploaded: %s -> s3://%s/%s", local_path, bucket, key)


def cmd_whoami(args: argparse.Namespace) -> int:
    """Display the identity of the selected AWS credentials.

    Parameters
    ----------
    args : argparse.Namespace
        Parsed command-line options.

    Returns
    -------
    int
        CLI exit status; zero indicates success.
    """
    sts = make_sts_client(args)
    identity = sts.get_caller_identity()
    print_table(
        ["Field", "Value"],
        [
            ["Account", identity.get("Account", "-")],
            ["Arn", identity.get("Arn", "-")],
            ["UserId", identity.get("UserId", "-")],
        ],
    )
    return 0


def cmd_check_access(args: argparse.Namespace) -> int:
    """Probe S3 access, optionally creating and deleting a temporary object.

    Parameters
    ----------
    args : argparse.Namespace
        Parsed command-line options.

    Returns
    -------
    int
        CLI exit status; zero indicates success.
    """
    s3 = make_s3_client(args)
    rows = []
    failed = False
    probes = [
        ("HeadBucket", lambda: s3.head_bucket(Bucket=args.bucket)),
        (
            "ListObjectsV2",
            lambda: s3.list_objects_v2(
                Bucket=args.bucket, Prefix=args.prefix, MaxKeys=1
            ),
        ),
    ]
    if args.key:
        probes.append(
            (
                "GetObject (first byte)",
                lambda: s3.get_object(
                    Bucket=args.bucket, Key=args.key, Range="bytes=0-0"
                ),
            )
        )
    for name, probe in probes:
        try:
            response = probe()
            if "Body" in response:
                response["Body"].close()
            rows.append([name, "allowed", "Request succeeded"])
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "Unknown")
            # Empty objects cannot satisfy a byte range; retry without a range.
            if name.startswith("GetObject") and code in {"InvalidRange", "416"}:
                try:
                    response = s3.get_object(Bucket=args.bucket, Key=args.key)
                    response["Body"].close()
                    rows.append(
                        [
                            name,
                            "allowed",
                            "Range unsupported; GetObject succeeded (body not read)",
                        ]
                    )
                    continue
                except ClientError as retry_exc:
                    exc = retry_exc
            rows.append([name, "failed", describe_access_error(exc, args.bucket)])
            failed = True
    if getattr(args, "write_check", False):
        # Never use --key for the write probe: it may name an existing object.
        test_key = args.prefix + f".s3ctl-access-check-{uuid4().hex}"
        logger.info("Write probe key: %s/%s", args.bucket, test_key)
        try:
            s3.put_object(
                Bucket=args.bucket,
                Key=test_key,
                Body=b"s3ctl access check\n",
                IfNoneMatch="*",
            )
        except (ClientError, BotoCoreError) as exc:
            rows.append(["PutObject", "failed", str(exc)])
            rows.append(["DeleteObject", "skipped", "PUT did not succeed"])
            logger.error(
                "PUT failed; inspect %s/%s if the request outcome is uncertain",
                args.bucket,
                test_key,
            )
            failed = True
        else:
            rows.append(["PutObject", "allowed", test_key])
            try:
                s3.delete_object(Bucket=args.bucket, Key=test_key)
            except (ClientError, BotoCoreError) as exc:
                rows.append(["DeleteObject", "failed", str(exc)])
                logger.error("Test object may remain: %s/%s", args.bucket, test_key)
                failed = True
            else:
                rows.append(["DeleteObject", "allowed", test_key])
                logger.info(
                    "Test object deleted; versioned buckets may retain its version "
                    "and a delete marker."
                )
    print_table(["Operation", "Result", "Details"], rows)
    logger.info("Results apply only to the selected credentials and tested resources.")
    if not getattr(args, "write_check", False):
        logger.info("Upload/delete permissions were not tested (use --write-check).")
    return 2 if failed else 0


def cmd_list_buckets(args: argparse.Namespace) -> int:
    """List all buckets visible to the selected credentials.

    Parameters
    ----------
    args : argparse.Namespace
        Parsed command-line options.

    Returns
    -------
    int
        CLI exit status; zero indicates success.
    """
    s3 = make_s3_client(args)
    buckets = [
        bucket
        for page in s3.get_paginator("list_buckets").paginate()
        for bucket in page.get("Buckets", [])
    ]
    rows = []
    for bucket in sorted(buckets, key=lambda item: item.get("Name", "")):
        name = bucket.get("Name", "-")
        rows.append(
            [
                name,
                bucket.get("BucketRegion") or get_bucket_region(s3, name),
                format_dt(bucket.get("CreationDate")),
            ]
        )

    if not rows:
        logger.info("Список бакетов пуст для выбранных credentials.")
        return 0

    print_table(["Bucket", "Region", "Creation date"], rows)
    return 0


def cmd_list_objects(args: argparse.Namespace) -> int:
    """List and sort the selected levels of a bucket or prefix.

    Parameters
    ----------
    args : argparse.Namespace
        Parsed command-line options.

    Returns
    -------
    int
        CLI exit status; zero indicates success.
    """
    s3 = make_s3_client(args)
    depth = None if args.recursive else (args.depth if args.depth is not None else 1)
    if depth is not None and depth < 1:
        raise ToolError("--depth must be at least 1")
    logger.info("Listing %s (depth: %s)", args.bucket, depth or "all")
    objects = list(iter_s3_listing(s3, args.bucket, args.prefix or "", depth))

    sorters = {
        "name": lambda obj: obj.get("Key", "").lower(),
        "last_modified": lambda obj: (
            obj.get("LastModified").timestamp() if obj.get("LastModified") else 0
        ),
        "size": lambda obj: obj.get("Size", 0),
    }
    objects.sort(key=sorters[args.sort_by], reverse=args.order == "desc")

    rows = [
        [
            obj.get("Key", "-"),
            format_dt(obj.get("LastModified")),
            "-" if obj.get("IsPrefix") else obj.get("Size", 0),
            "-" if obj.get("IsPrefix") else human_size(int(obj.get("Size", 0))),
            "PREFIX" if obj.get("IsPrefix") else obj.get("StorageClass", "STANDARD"),
        ]
        for obj in objects
    ]

    if not rows:
        logger.info("Объекты не найдены.")
        return 0

    print_table(
        ["Key", "Last modified", "Size bytes", "Size", "Storage class"],
        rows,
        pager=args.pager,
    )
    return 0


def cmd_download_bucket(args: argparse.Namespace) -> int:
    """Download current objects from a bucket or prefix, preserving their keys.

    Parameters
    ----------
    args : argparse.Namespace
        Parsed command-line options.

    Returns
    -------
    int
        CLI exit status; zero indicates success.
    """
    s3 = make_s3_client(args)
    count = 0
    destinations = {}
    for obj in track(
        iter_s3_objects(s3, args.bucket, args.prefix or ""),
        description="Downloading",
        console=console,
    ):
        count += int(
            download_one(
                s3,
                args.bucket,
                obj["Key"],
                args.output_dir,
                obj.get("Size"),
                destinations,
            )
        )
    logger.info("Downloaded %s files to %s", count, args.output_dir)
    return 0


def cmd_download(args: argparse.Namespace) -> int:
    """Download exact object keys or keys matching a glob.

    Parameters
    ----------
    args : argparse.Namespace
        Parsed command-line options.

    Returns
    -------
    int
        CLI exit status; zero indicates success.
    """
    s3 = make_s3_client(args)

    sizes = {}
    if args.key:
        keys = unique_keys(args.key)
    else:
        matches = find_objects_by_pattern(s3, args.bucket, args.pattern)
        keys = [obj["Key"] for obj in matches]
        sizes = {obj["Key"]: obj.get("Size") for obj in matches}

    if not keys:
        logger.info("Нет объектов для скачивания.")
        return 0

    count = 0
    destinations = {}
    for key in track(keys, description="Downloading", console=console):
        count += int(
            download_one(
                s3, args.bucket, key, args.output_dir, sizes.get(key), destinations
            )
        )
    logger.info("Downloaded %s files to %s", count, args.output_dir)
    return 0


def cmd_upload(args: argparse.Namespace) -> int:
    """Upload a regular file or the contents of a directory.

    Parameters
    ----------
    args : argparse.Namespace
        Parsed command-line options.

    Returns
    -------
    int
        CLI exit status; zero indicates success.
    """
    s3 = make_s3_client(args)
    local_path = Path(args.local_path).expanduser()

    if not local_path.exists():
        raise ToolError(f"Локальный путь не найден: {local_path}")

    if local_path.is_file():
        filename = args.s3_key if args.s3_key is not None else local_path.name
        prefix = args.prefix.strip("/")
        key = f"{prefix}/{filename}" if prefix else filename
        if not key:
            raise ToolError("Не удалось определить S3 key для загрузки файла.")
        upload_one(s3, args.bucket, local_path, key)
        logger.info("Готово. Загружен 1 файл.")
        return 0

    if not local_path.is_dir():
        raise ToolError(f"Local path is not a regular file or directory: {local_path}")

    base_prefix = join_s3_key(args.prefix, args.s3_key or "")
    files = sorted(path for path in local_path.rglob("*") if path.is_file())
    if not files:
        logger.info("В директории нет файлов для загрузки.")
        return 0

    for file_path in track(files, description="Uploading", console=console):
        relative_key = file_path.relative_to(local_path).as_posix()
        key = join_s3_key(base_prefix, relative_key)
        upload_one(s3, args.bucket, file_path, key)

    logger.info(f"Готово. Загружено файлов: {len(files)}")
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    """Delete selected objects only after explicit confirmation.

    Parameters
    ----------
    args : argparse.Namespace
        Parsed command-line options.

    Returns
    -------
    int
        CLI exit status; zero indicates success.
    """
    s3 = make_s3_client(args)

    if args.key:
        keys = unique_keys(args.key)
    else:
        keys = [
            obj["Key"] for obj in find_objects_by_pattern(s3, args.bucket, args.pattern)
        ]

    if not keys:
        logger.info("Нет объектов для удаления.")
        return 0

    if args.dry_run:
        logger.info("DRY RUN: будут удалены следующие объекты:")
        print_table(["Key"], [[key] for key in keys])
        return 0

    if not args.yes:
        raise ToolError(
            "Удаление не выполнено. Добавьте --yes для явного подтверждения или --dry-run для просмотра."
        )

    for key in track(keys, description="Deleting", console=console):
        response = s3.delete_object(Bucket=args.bucket, Key=key)
        logger.info(
            "DeleteObject succeeded: s3://%s/%s (delete marker: %s)",
            args.bucket,
            key,
            response.get("DeleteMarker", False),
        )

    logger.info("DeleteObject requests completed: %s", len(keys))
    return 0


def cmd_user_exists(args: argparse.Namespace) -> int:
    """Check whether an AWS IAM user exists in the selected account.

    Parameters
    ----------
    args : argparse.Namespace
        Parsed command-line options.

    Returns
    -------
    int
        CLI exit status; zero indicates success.
    """
    iam = make_iam_client(args)
    try:
        user = iam.get_user(UserName=args.user_name)["User"]
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "NoSuchEntity":
            logger.error("IAM user %s does not exist", args.user_name)
            return 2
        raise
    print_table(
        ["User", "ARN", "Created"],
        [[user["UserName"], user["Arn"], format_dt(user.get("CreateDate"))]],
    )
    return 0


def cmd_bucket_policy(args: argparse.Namespace) -> int:
    """Display the bucket's resource policy, if one is configured.

    Parameters
    ----------
    args : argparse.Namespace
        Parsed command-line options.

    Returns
    -------
    int
        CLI exit status; zero indicates success.
    """
    s3 = make_s3_client(args)
    try:
        response = s3.get_bucket_policy(Bucket=args.bucket)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "NoSuchBucketPolicy":
            logger.info("Bucket %s has no bucket policy", args.bucket)
            return 0
        raise
    show_json(f"Bucket policy: {args.bucket}", json.loads(response["Policy"]))
    return 0


def cmd_user_policy(args: argparse.Namespace) -> int:
    """Show inline, managed, group and boundary policies for an IAM user.

    Parameters
    ----------
    args : argparse.Namespace
        Parsed command-line options.

    Returns
    -------
    int
        CLI exit status; zero indicates success.
    """
    iam = make_iam_client(args)
    user = iam.get_user(UserName=args.user_name)["User"]
    count = 0

    def show_managed(arn: str, title: str) -> None:
        nonlocal count
        policy = iam.get_policy(PolicyArn=arn)["Policy"]
        document = iam.get_policy_version(
            PolicyArn=arn, VersionId=policy["DefaultVersionId"]
        )["PolicyVersion"]["Document"]
        show_json(title, document)
        count += 1

    def show_owner(kind: str, name: str) -> None:
        nonlocal count
        parameter = {f"{kind.title()}Name": name}
        for page in iam.get_paginator(f"list_{kind}_policies").paginate(**parameter):
            for policy_name in page.get("PolicyNames", []):
                document = getattr(iam, f"get_{kind}_policy")(
                    **parameter, PolicyName=policy_name
                )["PolicyDocument"]
                show_json(f"{kind} {name}: inline {policy_name}", document)
                count += 1
        for page in iam.get_paginator(f"list_attached_{kind}_policies").paginate(
            **parameter
        ):
            for policy in page.get("AttachedPolicies", []):
                show_managed(
                    policy["PolicyArn"],
                    f"{kind} {name}: managed {policy['PolicyName']}",
                )

    show_owner("user", args.user_name)
    for page in iam.get_paginator("list_groups_for_user").paginate(
        UserName=args.user_name
    ):
        for group in page.get("Groups", []):
            show_owner("group", group["GroupName"])
    boundary = user.get("PermissionsBoundary", {}).get("PermissionsBoundaryArn")
    if boundary:
        show_managed(boundary, "Permissions boundary (limits permissions)")
    logger.info(
        "Displayed %s policy documents; these do not prove effective S3 access", count
    )
    return 0


def cmd_user_access(args: argparse.Namespace) -> int:
    """Simulate IAM-user S3 permissions without executing writes or deletions.

    Parameters
    ----------
    args : argparse.Namespace
        Parsed command-line options.

    Returns
    -------
    int
        CLI exit status; zero indicates success.
    """
    iam = make_iam_client(args)
    user = iam.get_user(UserName=args.user_name)["User"]
    arn = user["Arn"]
    partition = arn.split(":")[1]
    bucket_arn = f"arn:{partition}:s3:::{args.bucket}"
    object_arn = f"{bucket_arn}/{args.key}" if args.key else f"{bucket_arn}/*"
    rows = []
    allowed = True
    for actions, resource in [
        (["s3:ListBucket"], bucket_arn),
        (["s3:GetObject", "s3:PutObject", "s3:DeleteObject"], object_arn),
    ]:
        for page in iam.get_paginator("simulate_principal_policy").paginate(
            PolicySourceArn=arn, ActionNames=actions, ResourceArns=[resource]
        ):
            for result in page.get("EvaluationResults", []):
                decision = result["EvalDecision"]
                missing = ", ".join(result.get("MissingContextValues", []))
                rows.append(
                    [
                        result["EvalActionName"],
                        result["EvalResourceName"],
                        decision,
                        missing or "-",
                    ]
                )
                allowed = allowed and decision == "allowed" and not missing
    print_table(["Action", "Resource", "IAM decision", "Missing context"], rows)
    logger.info(
        "SIMULATION ONLY: bucket policies are not included; object ACLs, "
        "KMS, endpoint policies and request context may change actual access. "
        "Use check-access with that user's profile for live read/list checks."
    )
    return 0 if allowed else 2
