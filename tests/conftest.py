"""Shared pytest configuration and fixtures for huawei_solar tests."""

from __future__ import annotations

import asyncio

import pytest


@pytest.fixture(scope="session")
def event_loop_policy():
    """Use the default asyncio event loop policy for all tests."""
    return asyncio.DefaultEventLoopPolicy()
