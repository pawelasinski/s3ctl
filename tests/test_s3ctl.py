"""Test CLI behavior with fake transfers and stubbed AWS responses."""

import json
from argparse import Namespace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import boto3
import pytest
from botocore.stub import Stubber

from s3ctl import cli, core, operations, ui


def client(service="s3"):
    return boto3.client(
        service,
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )


@pytest.mark.parametrize(
    "key", ["../secret", "/secret", "a/../secret", "a//b", "a/./b", "a\\b", ""]
)
def test_unsafe_download_keys(tmp_path, key):
    with pytest.raises(core.ToolError):
        core.safe_local_path(str(tmp_path), key)


def test_download_symlink_escape(tmp_path):
    outside = tmp_path.parent / "outside"
    outside.mkdir(exist_ok=True)
    (tmp_path / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(core.ToolError):
        core.safe_local_path(str(tmp_path), "linked/file.csv")


def test_paginated_case_sensitive_patterns():
    s3 = client()
    with Stubber(s3) as stub:
        stub.add_response(
            "list_objects_v2",
            {
                "IsTruncated": True,
                "NextContinuationToken": "next",
                "Contents": [{"Key": "data/a.csv", "Size": 1}],
            },
            {"Bucket": "bucket", "Prefix": "data/"},
        )
        stub.add_response(
            "list_objects_v2",
            {
                "IsTruncated": False,
                "Contents": [
                    {"Key": "data/b.CSV", "Size": 1},
                    {"Key": "data/c.csv", "Size": 1},
                ],
            },
            {"Bucket": "bucket", "Prefix": "data/", "ContinuationToken": "next"},
        )
        assert [
            o["Key"] for o in core.find_objects_by_pattern(s3, "bucket", "data/*.csv")
        ] == ["data/a.csv", "data/c.csv"]
        stub.assert_no_pending_responses()


def test_failed_download_preserves_existing_file(tmp_path):
    target = tmp_path / "data.csv"
    target.write_text("original")
    s3 = Mock()

    def fail(bucket, key, path):
        Path(path).write_text("partial")
        raise OSError("transfer failed")

    s3.download_file.side_effect = fail
    with pytest.raises(OSError):
        operations.download_one(s3, "bucket", "data.csv", str(tmp_path))
    assert target.read_text() == "original"
    assert list(tmp_path.glob(".s3ctl-*")) == []


def test_download_prefix_and_skip_marker(tmp_path, monkeypatch):
    s3 = Mock()
    monkeypatch.setattr(operations, "make_s3_client", lambda args: s3)
    monkeypatch.setattr(
        operations,
        "iter_s3_objects",
        lambda *args: iter(
            [{"Key": "reports/", "Size": 0}, {"Key": "reports/file.csv", "Size": 4}]
        ),
    )
    s3.download_file.side_effect = lambda bucket, key, path: Path(path).write_text(
        "data"
    )
    args = Namespace(bucket="bucket", prefix="reports/", output_dir=str(tmp_path))
    assert operations.cmd_download_bucket(args) == 0
    assert (tmp_path / "reports/file.csv").read_text() == "data"
    assert s3.download_file.call_count == 1


def test_upload_directory_preserves_relative_keys(tmp_path, monkeypatch):
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested/a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("b")
    s3 = Mock()
    monkeypatch.setattr(operations, "make_s3_client", lambda args: s3)
    args = Namespace(
        local_path=str(tmp_path), bucket="bucket", prefix="backup", s3_key=None
    )
    assert operations.cmd_upload(args) == 0
    keys = {call.args[2] for call in s3.upload_file.call_args_list}
    assert keys == {"backup/nested/a.txt", "backup/b.txt"}


def test_delete_requires_confirmation_and_dry_run_never_deletes(monkeypatch):
    s3 = Mock()
    monkeypatch.setattr(operations, "make_s3_client", lambda args: s3)
    args = Namespace(
        bucket="bucket", key=["a", "a"], pattern=None, dry_run=True, yes=False
    )
    assert operations.cmd_delete(args) == 0
    s3.delete_object.assert_not_called()
    args.dry_run = False
    with pytest.raises(core.ToolError):
        operations.cmd_delete(args)
    s3.delete_object.assert_not_called()
    args.yes = True
    assert operations.cmd_delete(args) == 0
    s3.delete_object.assert_called_once_with(Bucket="bucket", Key="a")


def test_user_not_found_vs_permission_denied(monkeypatch):
    iam = client("iam")
    monkeypatch.setattr(operations, "make_iam_client", lambda args: iam)
    args = Namespace(user_name="alice")
    with Stubber(iam) as stub:
        stub.add_client_error(
            "get_user",
            service_error_code="NoSuchEntity",
            expected_params={"UserName": "alice"},
        )
        assert operations.cmd_user_exists(args) == 2
        stub.add_client_error(
            "get_user",
            service_error_code="AccessDenied",
            expected_params={"UserName": "alice"},
        )
        from botocore.exceptions import ClientError

        with pytest.raises(ClientError):
            operations.cmd_user_exists(args)


def test_live_access_checks_are_independent(monkeypatch):
    s3 = client()
    monkeypatch.setattr(operations, "make_s3_client", lambda args: s3)
    rows = []
    monkeypatch.setattr(
        operations, "print_table", lambda headers, data: rows.extend(data)
    )
    with Stubber(s3) as stub:
        stub.add_client_error(
            "head_bucket",
            service_error_code="403",
            http_status_code=403,
            expected_params={"Bucket": "bucket"},
        )
        stub.add_response(
            "list_objects_v2",
            {"Contents": []},
            {"Bucket": "bucket", "Prefix": "allowed/", "MaxKeys": 1},
        )
        assert (
            operations.cmd_check_access(
                Namespace(bucket="bucket", prefix="allowed/", key=None)
            )
            == 2
        )
    assert [row[1] for row in rows] == ["failed", "allowed"]


def test_parser_defaults_and_selectors(monkeypatch):
    monkeypatch.setenv("S3_BUCKET", "default-bucket")
    monkeypatch.setenv("S3_ENDPOINT_URL", "http://localhost:9000")
    args = cli.build_parser().parse_args(["get", "--key", "a.csv"])
    assert args.bucket == "default-bucket"
    assert args.endpoint_url == "http://localhost:9000"
    assert args.output_dir == core.default_output_dir()
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["get", "--key", "a", "--pattern", "*.csv"])


def test_rich_table_and_json_are_logged(tmp_path):
    path = ui.setup_logging(tmp_path)
    ui.print_table(["Key"], [["data/file.csv"]])
    ui.show_json("policy", "%7B%22Statement%22%3A%20%5B%5D%7D")
    assert "data/file.csv" in path.read_text()
    assert '"Statement"' in path.read_text()
    for handler in ui.logger.handlers[:]:
        ui.logger.removeHandler(handler)
        handler.close()


def test_user_group_managed_and_boundary_policies(monkeypatch):
    iam = client("iam")
    monkeypatch.setattr(operations, "make_iam_client", lambda args: iam)
    documents = []
    monkeypatch.setattr(
        operations, "show_json", lambda title, doc: documents.append((title, doc))
    )
    managed = "arn:aws:iam::123456789012:policy/managed"
    boundary = "arn:aws:iam::123456789012:policy/boundary"
    policy = {"Version": "2012-10-17", "Statement": []}
    user = {
        "UserName": "alice",
        "UserId": "AIDAAAAAAAAAAAAAA",
        "Path": "/",
        "Arn": "arn:aws:iam::123456789012:user/alice",
        "CreateDate": datetime(2026, 1, 1, tzinfo=UTC),
        "PermissionsBoundary": {
            "PermissionsBoundaryType": "Policy",
            "PermissionsBoundaryArn": boundary,
        },
    }
    with Stubber(iam) as stub:
        stub.add_response("get_user", {"User": user}, {"UserName": "alice"})
        stub.add_response(
            "list_user_policies",
            {"PolicyNames": ["inline"], "IsTruncated": False},
            {"UserName": "alice"},
        )
        stub.add_response(
            "get_user_policy",
            {
                "UserName": "alice",
                "PolicyName": "inline",
                "PolicyDocument": json.dumps(policy),
            },
            {"UserName": "alice", "PolicyName": "inline"},
        )
        stub.add_response(
            "list_attached_user_policies",
            {
                "AttachedPolicies": [{"PolicyName": "managed", "PolicyArn": managed}],
                "IsTruncated": False,
            },
            {"UserName": "alice"},
        )
        stub.add_response(
            "get_policy", {"Policy": {"DefaultVersionId": "v1"}}, {"PolicyArn": managed}
        )
        stub.add_response(
            "get_policy_version",
            {"PolicyVersion": {"Document": json.dumps(policy)}},
            {"PolicyArn": managed, "VersionId": "v1"},
        )
        stub.add_response(
            "list_groups_for_user",
            {
                "Groups": [
                    {
                        "Path": "/",
                        "GroupName": "dev",
                        "GroupId": "AGPAAAAAAAAAAAAAA",
                        "Arn": "arn:aws:iam::123456789012:group/dev",
                        "CreateDate": user["CreateDate"],
                    }
                ],
                "IsTruncated": False,
            },
            {"UserName": "alice"},
        )
        stub.add_response(
            "list_group_policies",
            {"PolicyNames": ["group-inline"], "IsTruncated": False},
            {"GroupName": "dev"},
        )
        stub.add_response(
            "get_group_policy",
            {
                "GroupName": "dev",
                "PolicyName": "group-inline",
                "PolicyDocument": json.dumps(policy),
            },
            {"GroupName": "dev", "PolicyName": "group-inline"},
        )
        stub.add_response(
            "list_attached_group_policies",
            {"AttachedPolicies": [], "IsTruncated": False},
            {"GroupName": "dev"},
        )
        stub.add_response(
            "get_policy",
            {"Policy": {"DefaultVersionId": "v1"}},
            {"PolicyArn": boundary},
        )
        stub.add_response(
            "get_policy_version",
            {"PolicyVersion": {"Document": json.dumps(policy)}},
            {"PolicyArn": boundary, "VersionId": "v1"},
        )
        assert operations.cmd_user_policy(Namespace(user_name="alice")) == 0
        stub.assert_no_pending_responses()
    assert len(documents) == 4


def test_user_access_simulates_bucket_and_object_resources(monkeypatch):
    iam = client("iam")
    monkeypatch.setattr(operations, "make_iam_client", lambda args: iam)
    rows = []
    monkeypatch.setattr(
        operations, "print_table", lambda headers, data: rows.extend(data)
    )
    user_arn = "arn:aws:iam::123456789012:user/alice"
    bucket_arn = "arn:aws:s3:::bucket"
    object_arn = bucket_arn + "/reports/a.csv"
    user = {
        "UserName": "alice",
        "UserId": "AIDAAAAAAAAAAAAAA",
        "Path": "/",
        "Arn": user_arn,
        "CreateDate": datetime(2026, 1, 1, tzinfo=UTC),
    }
    with Stubber(iam) as stub:
        stub.add_response("get_user", {"User": user}, {"UserName": "alice"})
        stub.add_response(
            "simulate_principal_policy",
            {
                "EvaluationResults": [
                    {
                        "EvalActionName": "s3:ListBucket",
                        "EvalResourceName": bucket_arn,
                        "EvalDecision": "allowed",
                    }
                ]
            },
            {
                "PolicySourceArn": user_arn,
                "ActionNames": ["s3:ListBucket"],
                "ResourceArns": [bucket_arn],
            },
        )
        stub.add_response(
            "simulate_principal_policy",
            {
                "EvaluationResults": [
                    {
                        "EvalActionName": "s3:GetObject",
                        "EvalResourceName": object_arn,
                        "EvalDecision": "allowed",
                    },
                    {
                        "EvalActionName": "s3:PutObject",
                        "EvalResourceName": object_arn,
                        "EvalDecision": "implicitDeny",
                        "MissingContextValues": ["aws:SourceIp"],
                    },
                    {
                        "EvalActionName": "s3:DeleteObject",
                        "EvalResourceName": object_arn,
                        "EvalDecision": "explicitDeny",
                    },
                ]
            },
            {
                "PolicySourceArn": user_arn,
                "ActionNames": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
                "ResourceArns": [object_arn],
            },
        )
        assert (
            operations.cmd_user_access(
                Namespace(user_name="alice", bucket="bucket", key="reports/a.csv")
            )
            == 2
        )
        stub.assert_no_pending_responses()
    assert len(rows) == 4
    assert rows[2][3] == "aws:SourceIp"


def test_absent_bucket_policy_is_not_an_access_error(monkeypatch):
    s3 = client()
    monkeypatch.setattr(operations, "make_s3_client", lambda args: s3)
    with Stubber(s3) as stub:
        stub.add_client_error(
            "get_bucket_policy",
            service_error_code="NoSuchBucketPolicy",
            expected_params={"Bucket": "bucket"},
        )
        assert operations.cmd_bucket_policy(Namespace(bucket="bucket")) == 0


def test_download_success_preserves_key_path(tmp_path):
    s3 = Mock()
    s3.download_file.side_effect = lambda bucket, key, path: Path(path).write_text(
        "complete"
    )
    assert operations.download_one(s3, "bucket", "nested/file.csv", str(tmp_path))
    assert (tmp_path / "nested/file.csv").read_text() == "complete"
    assert not list((tmp_path / "nested").glob(".s3ctl-*"))


@pytest.mark.parametrize("encoded", [False, True])
def test_policy_percent_values_are_not_corrupted(tmp_path, encoded):
    from urllib.parse import quote

    document = {"Resource": "arn:aws:s3:::bucket/a%2Fb", "Value": "%22literal%22"}
    text = json.dumps(document)
    if encoded:
        text = quote(text)
    path = ui.setup_logging(tmp_path)
    try:
        ui.show_json("policy", text)
        logged = path.read_text()
        assert "a%2Fb" in logged
        assert "%22literal%22" in logged
    finally:
        for handler in ui.logger.handlers[:]:
            ui.logger.removeHandler(handler)
            handler.close()


def test_nonempty_trailing_slash_object_fails_without_false_success(tmp_path):
    s3 = Mock()
    with pytest.raises(core.ToolError, match="nonempty"):
        operations.download_one(s3, "bucket", "data/", str(tmp_path), size=10)
    s3.download_file.assert_not_called()
    s3.head_object.assert_not_called()


def test_exact_folder_key_is_checked_before_skipping(tmp_path):
    s3 = client()
    with Stubber(s3) as stub:
        stub.add_response(
            "head_object", {"ContentLength": 0}, {"Bucket": "bucket", "Key": "reports/"}
        )
        assert not operations.download_one(s3, "bucket", "reports/", str(tmp_path))
        stub.assert_no_pending_responses()


def test_iam_and_missing_object_errors_are_identified_correctly():
    from botocore.exceptions import ClientError

    missing = ClientError(
        {"Error": {"Code": "NoSuchKey"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
        "GetObject",
    )
    assert "key" in core.describe_client_error(missing)
    assert "key" in core.describe_access_error(missing, "bucket")
    iam = ClientError(
        {
            "Error": {"Code": "NoSuchEntity"},
            "ResponseMetadata": {"HTTPStatusCode": 404},
        },
        "GetUser",
    )
    assert "IAM" in core.describe_client_error(iam)
    assert "Bucket" not in core.describe_client_error(iam)


def test_installed_package_does_not_use_unrelated_ancestor_project(
    tmp_path, monkeypatch
):
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    (unrelated / "pyproject.toml").write_text("[project]\nname='other'\n")
    installed = unrelated / ".venv/lib/python3.14/site-packages/s3ctl/core.py"
    installed.parent.mkdir(parents=True)
    installed.touch()
    current = tmp_path / "current"
    current.mkdir()
    monkeypatch.setattr(core, "__file__", str(installed))
    monkeypatch.chdir(current)
    assert core.project_dir() == current


def test_log_open_error_prevents_operations(tmp_path, monkeypatch, capsys):
    from unittest.mock import patch

    command = Mock(return_value=0)
    monkeypatch.setattr(cli, "cmd_list_buckets", command)
    # Fail at the file-open stage after the directory has been created.
    with patch.object(
        ui, "RotatingFileHandler", side_effect=PermissionError("not writable")
    ):
        assert cli.main(["ls-buckets", "--log-dir", str(tmp_path)]) == 1
    command.assert_not_called()
    assert "Cannot initialize file logging" in capsys.readouterr().err


def test_upload_rejects_special_local_paths(tmp_path, monkeypatch):
    import os

    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    s3 = Mock()
    monkeypatch.setattr(operations, "make_s3_client", lambda args: s3)
    args = Namespace(local_path=str(fifo), bucket="bucket", prefix="", s3_key=None)
    with pytest.raises(core.ToolError, match="regular file or directory"):
        operations.cmd_upload(args)
    s3.upload_file.assert_not_called()


def test_failed_upload_is_not_logged_as_completed(tmp_path, monkeypatch):
    local = tmp_path / "a.csv"
    local.write_text("data")
    s3 = Mock()
    s3.upload_file.side_effect = OSError("failed")
    log = Mock()
    monkeypatch.setattr(operations, "logger", log)
    with pytest.raises(OSError):
        operations.upload_one(s3, "bucket", local, "a.csv")
    log.info.assert_not_called()


@pytest.mark.parametrize("key", ["/leading.csv", "trailing/", "normal.csv"])
def test_upload_preserves_explicit_file_key(tmp_path, monkeypatch, key):
    local = tmp_path / "a.csv"
    local.write_text("data")
    s3 = Mock()
    monkeypatch.setattr(operations, "make_s3_client", lambda args: s3)
    args = Namespace(local_path=str(local), bucket="bucket", prefix="", s3_key=key)
    assert operations.cmd_upload(args) == 0
    s3.upload_file.assert_called_once_with(str(local), "bucket", key)


def test_empty_upload_key_is_rejected(tmp_path, monkeypatch):
    local = tmp_path / "a.csv"
    local.write_text("data")
    s3 = Mock()
    monkeypatch.setattr(operations, "make_s3_client", lambda args: s3)
    args = Namespace(local_path=str(local), bucket="bucket", prefix="", s3_key="")
    with pytest.raises(core.ToolError):
        operations.cmd_upload(args)
    s3.upload_file.assert_not_called()


def test_download_rejects_different_keys_with_same_local_file(tmp_path):
    s3 = Mock()
    s3.download_file.side_effect = lambda bucket, key, path: Path(path).write_text(key)
    destinations = {}
    operations.download_one(
        s3, "bucket", "a.csv", str(tmp_path), destinations=destinations
    )
    # A hard link simulates two names referring to one existing filesystem object.
    (tmp_path / "alias.csv").hardlink_to(tmp_path / "a.csv")
    with pytest.raises(core.ToolError, match="collision"):
        operations.download_one(
            s3, "bucket", "alias.csv", str(tmp_path), destinations=destinations
        )
    assert (tmp_path / "a.csv").read_text() == "a.csv"
    assert s3.download_file.call_count == 1


def test_case_insensitive_download_collision(tmp_path):
    existing = tmp_path / "A.csv"
    existing.write_text("first")
    if not (tmp_path / "a.csv").exists():
        pytest.skip("requires a case-insensitive filesystem")
    s3 = Mock()
    s3.download_file.side_effect = lambda bucket, key, path: Path(path).write_text(key)
    destinations = {}
    operations.download_one(
        s3, "bucket", "A.csv", str(tmp_path), destinations=destinations
    )
    with pytest.raises(core.ToolError, match="collision"):
        operations.download_one(
            s3, "bucket", "a.csv", str(tmp_path), destinations=destinations
        )
    assert existing.read_text() == "A.csv"


@pytest.fixture
def configured_environment(tmp_path, monkeypatch):
    """Use synthetic credentials and an isolated AWS profile, with no network."""
    credentials = tmp_path / "credentials"
    credentials.write_text(
        "[example]\naws_access_key_id=profile-key\naws_secret_access_key=profile-secret\n"
    )
    config = tmp_path / "config"
    config.write_text("[profile example]\nregion=us-west-2\n")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(credentials))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_PROFILE", raising=False)
    monkeypatch.delenv("AWS_SECURITY_TOKEN", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "environment-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "environment-secret")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "environment-token")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-central-1")
    monkeypatch.setenv("S3_BUCKET", "environment-bucket")
    monkeypatch.setenv("S3_ENDPOINT_URL", "http://localhost:9000")
    monkeypatch.delenv("S3_ADDRESSING_STYLE", raising=False)


def test_environment_credentials_region_and_endpoint(configured_environment):
    args = cli.build_parser().parse_args(["ls"])
    session = core.make_session(args)
    credentials = session.get_credentials().get_frozen_credentials()
    assert credentials.access_key == "environment-key"
    assert credentials.secret_key == "environment-secret"
    assert credentials.token == "environment-token"
    assert session.region_name == "eu-central-1"
    assert args.bucket == "environment-bucket"
    assert core.make_s3_client(args).meta.endpoint_url == "http://localhost:9000"


def test_cli_overrides_environment_defaults(configured_environment):
    args = cli.build_parser().parse_args(
        [
            "ls",
            "--bucket",
            "cli-bucket",
            "--region",
            "us-east-1",
            "--endpoint-url",
            "https://s3.example.com",
            "--addressing-style",
            "path",
            "--access-key",
            "cli-key",
            "--secret-key",
            "cli-secret",
            "--session-token",
            "cli-token",
        ]
    )
    session = core.make_session(args)
    credentials = session.get_credentials().get_frozen_credentials()
    assert credentials.access_key == "cli-key"
    assert credentials.secret_key == "cli-secret"
    assert credentials.token == "cli-token"
    assert session.region_name == "us-east-1"
    assert args.bucket == "cli-bucket"
    s3 = core.make_s3_client(args)
    assert s3.meta.endpoint_url == "https://s3.example.com"
    assert s3.meta.config.s3["addressing_style"] == "path"


def test_explicit_profile_overrides_environment_keys(configured_environment):
    args = cli.build_parser().parse_args(["ls", "--profile", "example"])
    credentials = core.make_session(args).get_credentials().get_frozen_credentials()
    assert credentials.access_key == "profile-key"
    assert credentials.secret_key == "profile-secret"
    assert credentials.token is None


def test_environment_profile_uses_normal_credential_chain(
    configured_environment, monkeypatch
):
    monkeypatch.setenv("AWS_PROFILE", "example")
    args = cli.build_parser().parse_args(["ls"])
    assert core.make_session(args).get_credentials().access_key == "environment-key"
    for name in ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"]:
        monkeypatch.delenv(name)
    assert core.make_session(args).get_credentials().access_key == "profile-key"


def test_empty_s3_environment_values_use_normal_defaults(
    configured_environment, monkeypatch
):
    for name in ["S3_BUCKET", "S3_ENDPOINT_URL", "S3_ADDRESSING_STYLE"]:
        monkeypatch.setenv(name, "  ")
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["ls"])
    args = cli.build_parser().parse_args(["ls", "--bucket", "explicit-bucket"])
    assert args.endpoint_url is None
    assert args.addressing_style == "auto"


def test_existing_envrc_loads_local_values_last(tmp_path):
    import os
    import shutil
    import subprocess
    import sys

    direnv = shutil.which("direnv")
    if direnv is None:
        pytest.skip("requires direnv")
    (tmp_path / ".env").write_text(
        "S3_BUCKET=shared-bucket\nAWS_DEFAULT_REGION=eu-central-1\n"
    )
    (tmp_path / ".env.local").write_text("S3_BUCKET=local-bucket\n")
    script = 'eval "$("$1" stdlib)"; source "$2"; "$3" -c \'import json, os; print(json.dumps({k: os.environ[k] for k in ["S3_BUCKET", "AWS_DEFAULT_REGION", "VIRTUAL_ENV"]}))\''
    # Source the real .envrc in an isolated directory with synthetic values.
    result = subprocess.run(
        [
            "bash",
            "-e",
            "-c",
            script,
            "_",
            direnv,
            str(core.project_dir() / ".envrc"),
            sys.executable,
        ],
        cwd=tmp_path,
        env={**os.environ},
        capture_output=True,
        text=True,
        check=True,
    )
    values = json.loads(result.stdout)
    assert values["S3_BUCKET"] == "local-bucket"
    assert values["AWS_DEFAULT_REGION"] == "eu-central-1"
    assert values["VIRTUAL_ENV"] == str(tmp_path / ".venv")


@pytest.mark.parametrize("failure", [None, "put", "delete"])
def test_write_check_uses_only_unique_probe_key(monkeypatch, failure):
    from botocore.exceptions import ClientError

    s3 = Mock()
    s3.head_bucket.return_value = {}
    s3.list_objects_v2.return_value = {}
    s3.get_object.return_value = {"Body": Mock()}
    if failure:
        method = s3.put_object if failure == "put" else s3.delete_object
        method.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "Denied"}}, failure
        )
    monkeypatch.setattr(operations, "make_s3_client", lambda args: s3)
    rows = []
    monkeypatch.setattr(
        operations, "print_table", lambda headers, values: rows.extend(values)
    )
    args = cli.build_parser().parse_args(
        [
            "check-access",
            "--bucket",
            "bucket",
            "--prefix",
            "reports/",
            "--key",
            "existing.csv",
            "--write-check",
        ]
    )
    assert operations.cmd_check_access(args) == (2 if failure else 0)
    put = s3.put_object.call_args.kwargs
    assert put["Key"].startswith("reports/.s3ctl-access-check-")
    assert put["Key"] != args.key
    assert put["IfNoneMatch"] == "*"
    if failure == "put":
        s3.delete_object.assert_not_called()
        assert rows[-1][1] == "skipped"
    else:
        s3.delete_object.assert_called_once_with(Bucket="bucket", Key=put["Key"])
        assert rows[-1][1] == ("failed" if failure else "allowed")


def test_check_access_does_not_write_by_default(monkeypatch):
    s3 = Mock()
    s3.head_bucket.return_value = {}
    s3.list_objects_v2.return_value = {}
    monkeypatch.setattr(operations, "make_s3_client", lambda args: s3)
    monkeypatch.setattr(operations, "print_table", lambda *args: None)
    args = cli.build_parser().parse_args(["check-access", "--bucket", "bucket"])
    assert operations.cmd_check_access(args) == 0
    s3.put_object.assert_not_called()
    s3.delete_object.assert_not_called()


@pytest.mark.parametrize("depth", [1, 2])
def test_listing_depth_uses_server_side_delimiter(depth):
    s3 = client()
    with Stubber(s3) as stub:
        stub.add_response(
            "list_objects_v2",
            {
                "Contents": [{"Key": "reports/a.csv", "Size": 1}],
                "CommonPrefixes": [{"Prefix": "reports/sub/"}],
            },
            {"Bucket": "bucket", "Prefix": "reports/", "Delimiter": "/"},
        )
        if depth == 2:
            stub.add_response(
                "list_objects_v2",
                {
                    "Contents": [{"Key": "reports/sub/b.csv", "Size": 2}],
                    "CommonPrefixes": [{"Prefix": "reports/sub/deeper/"}],
                },
                {"Bucket": "bucket", "Prefix": "reports/sub/", "Delimiter": "/"},
            )
        result = list(core.iter_s3_listing(s3, "bucket", "reports/", depth))
        assert [obj["Key"] for obj in result] == (
            ["reports/a.csv", "reports/sub/"]
            if depth == 1
            else [
                "reports/a.csv",
                "reports/sub/",
                "reports/sub/b.csv",
                "reports/sub/deeper/",
            ]
        )
        stub.assert_no_pending_responses()


def test_listing_bucket_root_and_recursive():
    s3 = client()
    with Stubber(s3) as stub:
        stub.add_response(
            "list_objects_v2",
            {"CommonPrefixes": [{"Prefix": "sub/"}]},
            {"Bucket": "bucket", "Prefix": "", "Delimiter": "/"},
        )
        assert list(core.iter_s3_listing(s3, "bucket")) == [
            {"Key": "sub/", "IsPrefix": True}
        ]
        stub.add_response(
            "list_objects_v2",
            {"Contents": [{"Key": "sub/a.csv"}]},
            {"Bucket": "bucket"},
        )
        assert list(core.iter_s3_listing(s3, "bucket", depth=None)) == [
            {"Key": "sub/a.csv"}
        ]


def test_listing_flags_and_invalid_depth(monkeypatch):
    parser = cli.build_parser()
    args = parser.parse_args(["ls", "--bucket", "bucket"])
    assert not args.recursive and args.depth is None
    with pytest.raises(SystemExit):
        parser.parse_args(["ls", "--bucket", "bucket", "--recursive", "--depth", "2"])
    args.depth = 0
    monkeypatch.setattr(operations, "make_s3_client", lambda args: Mock())
    with pytest.raises(core.ToolError, match="depth"):
        operations.cmd_list_objects(args)
