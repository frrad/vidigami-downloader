# Vidigami Downloader

Vidigami Downloader is a privacy-first, local command-line tool for downloading
media that a user is authorized to access and for preserving the media's
relationships over time.

The project records opaque Vidigami IDs for page/container membership and
person tags. Selection is derived from the current configuration at sync time;
changing a page or tagged-user filter does not rewrite historical metadata.

## Status

This repository is an early scaffold. Authentication, API enumeration, and
download behavior are being implemented incrementally. The command names are
available so that integrations and documentation can be developed against a
stable CLI surface.

## Privacy

Credentials, local configuration, databases, downloaded media, metadata,
reports, logs, and local privacy patterns are intentionally ignored by Git.
Use [`config.example.toml`](config.example.toml) as a synthetic template and
copy it to the ignored `config.toml` file for local use. Do not commit real
organization, page, user, or account information.

Before committing, run:

```console
python scripts/privacy_check.py
```

The check scans tracked files and staged candidate files using patterns from
the ignored `.privacy-patterns` file when present.

## Installation

Python 3.12 or newer is required.

```console
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

## CLI scaffold

```console
vidigami auth login
vidigami auth status
vidigami relationships
vidigami doctor
vidigami sync --dry-run
vidigami status
vidigami report
vidigami verify
```

The current scaffold reports that these operations are not yet implemented.
It does not access an account or network service.

## License

MIT. See [`LICENSE`](LICENSE).
