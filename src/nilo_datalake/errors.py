"""Errors raised by the archive service."""


class DatalakeError(Exception):
    """Base class for expected datalake failures."""


class ConfigError(DatalakeError):
    """The configuration cannot be used as written."""


class InvalidPathError(DatalakeError):
    """A caller-supplied path is empty, absolute, or escapes its root."""


class ChecksumMismatchError(DatalakeError):
    """Bytes on disk do not match the checksum declared by the sender."""


class StorageFullError(DatalakeError):
    """No enabled volume has enough free space for this write."""


class BatchStateError(DatalakeError):
    """The batch is missing or is not in the state the operation requires."""


class ManifestError(DatalakeError):
    """A drop-folder manifest is missing or does not describe its files."""
