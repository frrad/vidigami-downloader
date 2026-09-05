# Vidigami Downloader

An unofficial, privacy-first command-line archive tool for media you are
authorized to access in Vidigami.

It uses Vidigami's current OAuth/OIDC and GraphQL surface—without browser
automation, browser-profile access, or cookie scraping. The API is undocumented
and may change.

## What it selects

Each sync takes the union of:

- every media item found in the configured page IDs, including untagged media;
- every media item tagged with any configured user ID, even outside those pages.

The SQLite state remains canonical rather than baking in that filter. For each
media item it records actual container/page IDs and face-tag ID plus tagged-user
ID pairs, with observation timestamps. Reports derive the current selection from
those IDs, so changing the configured pages or tagged users does not rewrite the
underlying metadata.

Google Photos upload is intentionally out of scope. The archive directory is
ordinary local files that another tool can import.

For optional local, semi-automatic review and person tagging of downloaded
photos, see the suggested companion project [`frrad/photo-person-review`](https://github.com/frrad/photo-person-review).

## Privacy model

The public repository contains code and synthetic examples only. Git ignores:

- `config.toml` and other local configuration;
- credentials, tokens, and local privacy patterns;
- SQLite state, downloaded media, reports, metadata, and logs.

Tokens are stored in the operating-system keychain. Downloads use hashed local
filenames, and signed download URLs are neither stored nor printed. Reports
contain opaque IDs, not names, email addresses, or URLs.

Before every commit, run:

```console
python scripts/privacy_check.py
```

For extra local protection, create an ignored `.privacy-patterns` file with one
regular expression per private value or pattern to reject.

## Installation

Python 3.12 or newer is required.

```console
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
cp config.example.toml config.toml
```

Edit only the ignored `config.toml`. Use opaque Vidigami IDs, not display names.
The production OAuth client currently uses `client_secret_basic`; if the
provider requires it, put the authorized client secret only in local config.

## Usage

```console
vidigami doctor
vidigami auth login
vidigami auth status
vidigami relationships
vidigami pages
vidigami sync --dry-run
vidigami sync
vidigami status
vidigami report
vidigami verify
vidigami metadata
```

`auth login` uses Vidigami's direct HTML sign-in flow and prompts for the
username and password without storing either. It retains cookies only in
memory, keeps the OAuth Authorization Code + PKCE state in memory, and stops
before requesting the registered `https://app.vidigami.com/auth-callback`
redirect. Use `auth login --browser` to try the system-browser loopback flow.
Subsequent scheduled runs use refresh tokens from the OS keychain when the
provider issues one.

`sync` first completes page and tagged-user enumeration and reconciles those
source IDs in SQLite. Deep metadata, container, and face-tag hydration is then
limited to new items and prior hydration failures; successfully hydrated old
items are not rehydrated on later runs. It then atomically downloads the
selected union. A temporary file is checksummed and fsynced before rename.
Re-running is idempotent and verifies/reuses existing archive files.

`pages` lists every page in the configured `space_id` using the GraphQL API.
Its JSON output contains each page's opaque ID and display name, for example
`{"pages":[{"id":"page|example","name":"Example Page"}]}`. Names are
returned only by this explicit listing command and are not stored in
configuration, SQLite state, or reports. The command follows API cursors until
the complete list is returned.

`metadata` is a local-only, idempotent backfill for completed downloads. It
uses the original file bytes to identify MIME type and image dimensions, and
uses EXIF `DateTimeOriginal` (then `DateTimeDigitized`) for `captured_at`.
EXIF timestamps without an offset remain timezone-unknown wall-clock values;
filesystem times and Vidigami `createdAt` are never treated as camera capture
time. Unsupported formats such as HEIC remain safely unfilled until a decoder
is available. The command neither contacts Vidigami nor redownloads files.

## Scheduling on macOS

For periodic unattended runs, use the included launchd setup. It installs a
per-user agent that runs daily at 18:15 local time, and once at login after a
shutdown or missed run. Each invocation runs from the repository root and
executes `sync`, `report --format both`, and `verify` in a fail-fast sequence.
The generated plist contains your local repository path and lives outside this
repository:

```console
./scripts/launchd-install.sh
```

Output is written to the ignored `logs/launchd.out.log` and
`logs/launchd.err.log`. To remove the schedule, run:

```console
./scripts/launchd-uninstall.sh
```

The agent runs as your macOS user, so it can use the refresh token in that
user's Keychain. If the token expires or is revoked, run `vidigami auth login`
interactively again; the scheduler cannot complete an interactive login.

## Development and CI

```console
ruff check .
mypy src
python scripts/privacy_check.py
pytest
```

GitHub Actions runs all four checks on pushes and pull requests, using Python
3.12 and strict mypy settings.

## Prior art

[`cytocracy/vidigami`](https://github.com/cytocracy/vidigami) demonstrated a
legacy cookie-and-CDN approach in 2023. This project does not reuse its exposed
cookie pattern or URL construction; it targets the current bearer-authenticated
GraphQL API and asks Vidigami for fresh download URLs.

## Responsible use

Use this only with an account and media you are authorized to access. Keep the
archive private, respect the school's policies and Vidigami's terms, and avoid
aggressive request rates.

## License

MIT. See [`LICENSE`](LICENSE).
