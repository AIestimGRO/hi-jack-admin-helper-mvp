"""Playwright defaults for JACKSIDE e2e."""

from __future__ import annotations

import pytest


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    return {
        **browser_context_args,
        "locale": "ru-RU",
        "viewport": {"width": 1280, "height": 720},
    }
