from __future__ import annotations

import pytest

from nilo_datalake.config import Settings
from nilo_datalake.console.archive_probe import try_archive_action
from nilo_datalake.errors import ConfigError


def test_unknown_archive_action() -> None:
    with pytest.raises(ConfigError, match="unknown"):
        try_archive_action("nope", Settings())
