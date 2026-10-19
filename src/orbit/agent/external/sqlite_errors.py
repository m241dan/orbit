"""What orbit makes of the errors SQLite and the operating system raise."""

import errno
import sqlite3

from orbit.agent.domain.storage_halt import StorageErrorKind

_SQLITE_KINDS = {
    sqlite3.SQLITE_FULL: StorageErrorKind.DISK_FULL,
    sqlite3.SQLITE_READONLY: StorageErrorKind.READ_ONLY,
    sqlite3.SQLITE_PERM: StorageErrorKind.READ_ONLY,
    sqlite3.SQLITE_BUSY: StorageErrorKind.LOCKED,
    sqlite3.SQLITE_LOCKED: StorageErrorKind.LOCKED,
    sqlite3.SQLITE_CANTOPEN: StorageErrorKind.MISSING,
}
"""What orbit makes of SQLite's primary result codes."""

_EXTENDED_KINDS = {sqlite3.SQLITE_READONLY_DBMOVED: StorageErrorKind.MISSING}
"""What orbit makes of the extended result codes that mean more than their primary code."""

_ERRNO_KINDS = {
    errno.ENOSPC: StorageErrorKind.DISK_FULL,
    errno.EDQUOT: StorageErrorKind.DISK_FULL,
    errno.EROFS: StorageErrorKind.READ_ONLY,
    errno.EACCES: StorageErrorKind.READ_ONLY,
    errno.EPERM: StorageErrorKind.READ_ONLY,
    errno.ENOENT: StorageErrorKind.MISSING,
}
"""What orbit makes of the operating system's errors."""


def error_kind(error: sqlite3.Error | OSError) -> StorageErrorKind:
    """What orbit makes of ``error``, by its SQLite result code or its ``errno``."""
    if isinstance(error, OSError):
        return _ERRNO_KINDS.get(error.errno or 0, StorageErrorKind.UNKNOWN)
    code = getattr(error, "sqlite_errorcode", None)  # Unset on errors SQLite didn't raise.
    if not isinstance(code, int):
        return StorageErrorKind.UNKNOWN
    return _EXTENDED_KINDS.get(code) or _SQLITE_KINDS.get(code & 0xFF, StorageErrorKind.UNKNOWN)
