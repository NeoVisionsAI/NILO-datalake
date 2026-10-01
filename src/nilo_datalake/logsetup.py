"""Backward-compatible import. New code uses ``nilo_datalake.tracing``."""

from nilo_datalake.tracing import configure_logging

__all__ = ["configure_logging"]
