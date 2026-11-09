"""Read optional local login credentials without changing the process environment."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values


def login_credentials(path: Path) -> tuple[str | None, str | None]:
    """Environment variables override the .env next to the selected config.

    Disable interpolation so dollar signs in passwords remain literal. Missing
    or empty values allow interactive login to prompt for the missing field.
    """
    values = dotenv_values(path, interpolate=False) if path.is_file() else {}
    return (
        os.environ.get("VIDIGAMI_USERNAME", values.get("VIDIGAMI_USERNAME")) or None,
        os.environ.get("VIDIGAMI_PASSWORD", values.get("VIDIGAMI_PASSWORD")) or None,
    )
