"""Bounded provider adapters. Provider errors never include credentials or raw traces."""

from app.providers.common import ProviderError

__all__ = ["ProviderError"]

