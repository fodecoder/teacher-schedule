"""Shared fixtures: load the anonymised example config once per session."""

from __future__ import annotations

from pathlib import Path

import pytest

import data

EXAMPLE_CONFIG = Path(__file__).resolve().parents[1] / "config.example.yaml"


@pytest.fixture(scope="session")
def config() -> data.Config:
    """The validated :class:`data.Config` from ``config.example.yaml``.

    Loading also publishes the flat ``data.*`` module attributes, so tests can
    read either the returned object or ``data.COURSES`` & co.
    """
    return data.load_config(EXAMPLE_CONFIG)
