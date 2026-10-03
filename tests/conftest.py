from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from te_app_creator.spec import load_defaults, load_schema  # noqa: E402

EXAMPLES = sorted((ROOT / "examples").glob("*.yml"))


@pytest.fixture(scope="session")
def schema():
    return load_schema()


@pytest.fixture(scope="session")
def defaults():
    return load_defaults()
