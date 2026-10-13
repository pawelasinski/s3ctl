# s3ctl

A small CLI for Amazon S3 and S3-compatible storage.

## Setup

Choose either installation method. Python must satisfy `requires-python` in
`pyproject.toml`.

### Using uv

Install uv using the [official installation guide]([https://docs.astral.sh/uv/getting-started/installation/]).

Install the application and its runtime dependencies:

```sh
uv sync --no-dev
source .venv/bin/activate
```

For development, use `uv sync` to include development tools.

### Using pip

Create and activate the environment, then install the application:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
```

### Run

In subsequent sessions, activate the existing environment:

```sh
source .venv/bin/activate
```

Run the application:

```sh
s3ctl --help
s3ctl ls-buckets
```

The examples below assume the environment is activated. If using uv, you can
also run commands without activation using `uv run s3ctl ...`.

> **Note — optional direnv setup:** `.envrc` configures automatic environment
> activation and loading of `.env` files when you enter the project directory.
> It is not required for manual activation or `uv run`.
>
> On macOS, install direnv:
>
> ```sh
> brew install direnv
> ```
>
> On Linux, use the command for your distribution (e.g. Ubuntu / Debian):
>
> ```sh
> sudo apt update && sudo apt install direnv
> ```
>
> Enable the hook for your shell (choose one). For zsh on macOS or Linux:
>
> ```sh
> touch ~/.zshrc
> grep -qxF 'eval "$(direnv hook zsh)"' ~/.zshrc ||
>   echo 'eval "$(direnv hook zsh)"' >> ~/.zshrc
> eval "$(direnv hook zsh)"
> ```
>
> For bash on Linux, add the hook to `~/.bashrc` once:
>
> ```sh
> touch ~/.bashrc
> grep -qxF 'eval "$(direnv hook bash)"' ~/.bashrc ||
>   echo 'eval "$(direnv hook bash)"' >> ~/.bashrc
> eval "$(direnv hook bash)"
> ```
>
> Ensure the project's `.envrc` contains:
>
> ```sh
> export VIRTUAL_ENV="$PWD/.venv"
> PATH_add "$VIRTUAL_ENV/bin"
>
> dotenv_if_exists .env
> dotenv_if_exists .env.local
> ```
>
> In the project directory, authorize the file:
>
> ```sh
> direnv allow
> ```
>
> The existing `.venv` is now activated automatically on entry and the  
> environment changes are reverted on exit. Run `direnv allow` again after  
> editing `.envrc`. For this setup, create `.venv` first using either method above.

### Environment settings

Copy the template only if `.env.local` is not already configured:

```sh
cp .env.example .env.local
# Replace the example values with your settings.
```

Activating `.venv` does not load `.env` or `.env.local`. The application reads
exported environment variables and AWS profiles, not these files directly.
To load trusted files in a bash or zsh session:

```sh
set -a
source .env
source .env.local
set +a
```

These files must use shell-compatible assignments. `.env.local` overrides
values from `.env`; variables not defined in either file keep their existing
shell values. `.env.example` is a template and is not loaded.

Alternatively, uv can load the files for one command:

```sh
uv run --env-file .env --env-file .env.local s3ctl ls --pager
```

`ls --pager` uses the bucket configured in `S3_BUCKET`; `--bucket` overrides it.

## Configuration and overrides

The variables in `.env.example` are supported as follows:


| Variable                | Purpose                                                                                      |
| ----------------------- | -------------------------------------------------------------------------------------------- |
| `AWS_ACCESS_KEY_ID`     | Access key ID, paired with the secret key.                                                   |
| `AWS_SECRET_ACCESS_KEY` | Secret key for key-based authentication.                                                     |
| `AWS_DEFAULT_REGION`    | AWS region; `--region` overrides it.                                                         |
| `S3_BUCKET`             | Default bucket; `--bucket` overrides it.                                                     |
| `AWS_SESSION_TOKEN`     | Optional token for temporary AWS credentials.                                                |
| `AWS_PROFILE`           | Optional existing AWS profile; use as an alternative to environment keys.                    |
| `S3_ENDPOINT_URL`       | Optional endpoint for S3-compatible storage; `--endpoint-url` overrides it.                  |
| `S3_ADDRESSING_STYLE`   | Addressing style: `auto` (default), `path`, or `virtual`; `--addressing-style` overrides it. |

Temporary credentials require all three AWS key/token variables. For long-lived
keys, omit the session token. For profile authentication, comment out/remove
access-key, secret-key and session-token assignments from both environment
files, and ensure those variables are not still exported in your shell.
A profile must already exist in `~/.aws/config` or `~/.aws/credentials` (or other
AWS-configured locations). With no CLI credentials, boto3's normal credential
chain applies: environment keys take precedence over an environment-selected
profile. An explicit `--profile` selects that profile instead of environment keys.

CLI options go after the command name:

```sh
s3ctl ls --bucket another-bucket
s3ctl ls-buckets --profile dev --region eu-central-1
s3ctl ls --endpoint-url http://localhost:9000 --addressing-style path
```

Explicit `--access-key` and `--secret-key` must be provided together; add
`--session-token` for temporary credentials. They cannot be combined with an
explicit `--profile`. If no credentials are configured here, boto3 can still
use its other standard credential providers.

Addressing style defaults to `auto`. Use `--addressing-style path` for
S3-compatible servers that require it, or set `S3_ADDRESSING_STYLE` to
`auto`, `path`, or `virtual`. Empty S3 bucket/endpoint defaults are treated
as unset.

## Buckets and objects

```sh
s3ctl ls-buckets
s3ctl ls --bucket my-bucket
s3ctl ls --bucket my-bucket --prefix reports/ --sort-by size --order desc
s3ctl ls --bucket my-bucket --pager
```

`ls` defaults to the first level: objects and grouped prefixes (shown as
`PREFIX`). It uses S3 `Delimiter="/"` without scanning deeper objects.
`--depth N` includes N levels relative to the bucket root or the literal
`--prefix`; use a trailing slash for folder prefixes. `--recursive` lists all
objects at all levels. These two options cannot be combined.

```sh
s3ctl ls --bucket my-bucket --depth 2
s3ctl ls --bucket my-bucket --prefix reports/ --depth 2
s3ctl ls --bucket my-bucket --recursive
```

Sorting defaults to name ascending; size and last-modified sorting are also
supported. Grouped prefixes have no size or date. The selected listing is
collected before sorting and output, so a large recursive listing can still
be slow and consume memory. `--pager` opens the result for terminal scrolling
(Space/down to advance, `q` to leave); redirected output is printed normally.

## Identity and access

```sh
s3ctl whoami --profile dev
s3ctl check-access --bucket my-bucket --profile alice
s3ctl check-access --bucket my-bucket --profile alice --prefix reports/ --key reports/a.csv
s3ctl check-access --bucket my-bucket --profile alice --prefix reports/ --write-check
s3ctl user-exists --user-name alice --profile admin
s3ctl user-policy --user-name alice --profile admin
s3ctl bucket-policy --bucket my-bucket
s3ctl user-access --user-name alice --bucket my-bucket --key reports/a.csv --profile admin
```

- `check-access` sends independent `HeadBucket` and one-item `ListObjectsV2`
requests with the selected credentials. `--key` also probes `GetObject` with
a one-byte range. By default, it does not upload or delete anything and does not prove
write/delete permissions. A denied HEAD request cannot reliably distinguish
a missing bucket from restricted access.
With `--write-check`, it also tests PUT and DELETE using a unique temporary
object under `--prefix` (or the bucket root), never the object in `--key`.
A conditional PUT prevents overwriting an existing object. DELETE is attempted
only after PUT succeeds. If deletion fails, the test key is reported for cleanup;
a network error can leave the PUT outcome uncertain. On versioned buckets,
deletion may leave the test version and a delete marker. Results apply only
to the tested resources, not every object in the bucket.
- A profile named `alice` must already contain that user's actual credentials;
choosing an IAM user name does not switch identities.
- `user-exists` reports absence only for IAM `NoSuchEntity`. Lack of IAM permission
is reported as an error, not as a nonexistent user.
- `user-policy` shows inline and managed user/group policies and a permissions
boundary, if present. Reading these documents requires IAM permissions.
A boundary limits permissions; it does not grant them. Organization, session
and resource policies are not displayed.
- `bucket-policy` displays the bucket's resource policy as JSON using the S3
`GetBucketPolicy` API. It requires permission to read that policy. If no policy
is configured, the command reports this and exits successfully. Access errors
are reported as errors. The command only reads the policy; it does not change
it or determine a user's effective permissions.
- `user-access` uses the AWS IAM policy simulator for List/Get/Put/Delete actions.
Its results are a simulation, not proof of effective access. Bucket policy is
not included; ACLs, KMS permissions, endpoint policies and missing request context
can change the live result. Without `--key`, object actions use `bucket/*`.
IAM interprets `*` and `?` in resource ARNs as wildcards, including in `--key`;
avoid them when checking a particular object. Exit code `0` means all four
simulated actions were allowed, not that live bucket access was proven.
- `whoami` uses AWS STS. All `user-*` commands use AWS IAM. `--endpoint-url`  
configures S3 only; it does not connect to MinIO's user administration API.

## Download

```sh
# Entire bucket, streamed page by page
s3ctl get-bucket --bucket my-bucket --output-dir ./downloads

# All objects under a prefix
s3ctl get-bucket --bucket my-bucket --prefix reports/ --output-dir ./downloads

# Exact keys or a case-sensitive glob against full object keys
s3ctl get --bucket my-bucket --key reports/a.csv reports/b.csv --output-dir ./downloads
s3ctl get --bucket my-bucket --pattern 'reports/*.csv' --output-dir ./downloads

# No path: download to <project>/tmp/
s3ctl get --bucket my-bucket --key reports/a.csv
```

Keys retain their full paths: `reports/a.csv` becomes
`<output-dir>/reports/a.csv`, including when a prefix is selected. S3 prefixes
are strings, so use `reports/` to avoid matching `reports-old/`.
Patterns use Python fnmatch semantics: `*` can match `/` as well. Quote patterns
so the shell does not expand them first.

Only zero-byte trailing-slash folder markers are skipped. A nonempty object
whose key ends in `/` cannot be mapped to a regular file and causes an error,
rather than a successful but incomplete download. Unsafe local paths are rejected.
Downloads use a temporary sibling file and replace the destination only after success. Existing
files at the same destination are replaced. If different keys in one command
map to the same local file (for example `A.csv` and `a.csv` on a case-insensitive
filesystem), the command stops before replacing the earlier downloaded file.
A small registry of downloaded file identities is retained for this check.
This downloads current objects;
previous versions and delete markers are not exported. The project's `tmp/`
is persistent until you remove it. When installed outside the source checkout,
the default is `<current-working-directory>/tmp/`; ancestor projects are not selected.

## Upload

```sh
s3ctl put --bucket my-bucket --local-path ./report.csv --prefix reports
s3ctl put --bucket my-bucket --local-path ./report.csv --s3-key reports/latest.csv
s3ctl put --bucket my-bucket --local-path ./reports --prefix backup
```

Directory uploads preserve paths relative to that directory. For example,
`./reports/2026/a.csv` becomes `backup/2026/a.csv`. The source directory name is
not added automatically. Empty directories are not uploaded. For a file,
`--s3-key` is preserved exactly, including boundary slashes; a normalized
`--prefix`, if supplied, is prepended. An empty destination key is rejected.
Existing S3 keys are overwritten (or get a new version if bucket versioning
is enabled). No upload patterns are used.

## Delete

```sh
s3ctl delete --bucket my-bucket --pattern 'tmp/*' --dry-run
s3ctl delete --bucket my-bucket --pattern 'tmp/*' --yes
s3ctl delete --bucket my-bucket --key obsolete.csv --yes
```

Deletion requires `--yes`; `--dry-run` only displays the selected keys. Deletion
uses `DeleteObject` without a version ID: on a versioned bucket it normally
creates a delete marker, not a purge of all versions. A successful DeleteObject
request also does not prove the key existed before deletion. Commands run
sequentially; if one transfer/delete fails, earlier completed operations are not rolled back.

## Modules

- `cli.py`: arguments, help, dispatch and exit codes.
- `core.py`: AWS clients, object pagination, patterns and path validation.
- `operations.py`: S3 and IAM commands.
- `ui.py`: Rich tables, JSON, console/file logs.
- `__main__.py`: `python -m s3ctl` entry point.

## Logging

```sh
s3ctl ls-buckets --log-dir ./custom-logs --verbose
```

Logs are shown with Rich and saved to `<project>/logs/s3ctl.log`, rotated at 5 MB
with three backups. Logging is initialized only after arguments are parsed;
imports and `--help` do not create logs. If the log file cannot be opened, the command stops
before any S3 or IAM operation.
Listings, policy documents and completed transfer results are also saved.
Sizes displayed as KiB/MiB/GiB use multiples of 1024 bytes. Credentials and
full command arguments are not logged. In an installed package without the
source project, the default log directory is `<current-working-directory>/logs/`.
Rich progress counts processed items (including skipped folder markers);
it is not a byte-level progress bar.

## Help

```sh
s3ctl --help
s3ctl get --help
```

Use `--help` to list commands or view options for a specific command.

## Exit codes

Exit codes: `0` success, `1` operation error, `2` failed access/user checks (or
argparse usage errors), `130` interrupted operation.

## Checks

```sh
uv run ruff check src tests
uv run ruff format --check src tests
uv run pytest
```

## Remote server

Replace `user@server` with your SSH login and server address.
SSH and rsync must be available on both machines.

### Transfer the project

Run locally from the project directory:

```sh
ssh user@server 'mkdir -p ~/s3ctl'

rsync -avz --no-owner --no-group \
  --include '.env.example' \
  --exclude-from=.rsync-exclude \
  ./ user@server:src/s3ctl/
```

The destination is `~/s3ctl` on the server. Add `--dry-run` to preview changes.

### Install and run

Connect to the server. Install uv using the
[official guide](https://docs.astral.sh/uv/getting-started/installation/)
if needed.

```sh
ssh user@server
cd ~/s3ctl
uv sync --no-dev
source .venv/bin/activate

cp .env.example .env.local
chmod 600 .env.local
vim .env.local
```

Replace example settings with your credentials, then run:

```sh
set -a
source .env.local
set +a

s3ctl ls-buckets
s3ctl get-bucket --bucket my-bucket --output-dir ./tmp
exit
```

### Fetch downloaded files

Run locally:

```sh
mkdir -p ./downloads
rsync -avz --no-owner --no-group \
  user@server:s3ctl/tmp/ ./downloads/
```

Remote files remain in place. Existing local files may be updated.

### Remove the project and uv

Run on the server after fetching any downloaded files you want to keep.
This assumes uv was installed only for this project, using default paths.

```sh
deactivate 2>/dev/null || true
cd ~

# Remove the project, environment, settings, logs and downloads
rm -rf -- "$HOME/src/s3ctl"

# Remove uv-installed tools, Python versions and cached dependencies
uv tool uninstall --all
uv python uninstall --all
uv cache clean

# Remove uv data, configuration and executables
rm -rf -- "$HOME/.cache/uv" "$HOME/.local/share/uv" "$HOME/.config/uv"
rm -f -- "$HOME/.local/bin/uv" "$HOME/.local/bin/uvx"
```

Remove shell configuration lines added by the uv installer, such as
`. "$HOME/.local/bin/env"`, and any uv/uvx completion lines you added.
Delete `~/.local/bin/env` and `env.fish` only if this uv installation created
them. Keep the `~/.local/bin` directory and any pre-existing PATH settings.

System Python is unaffected. For custom paths, adjust the commands.
See the [official uninstall guide](https://docs.astral.sh/uv/getting-started/installation/#uninstallation).
