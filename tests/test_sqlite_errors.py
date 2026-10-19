"""What orbit makes of the errors SQLite and the operating system raise."""

import errno
import sqlite3

import pytest

from orbit.agent.domain.storage_halt import StorageErrorKind
from orbit.agent.external.sqlite_errors import error_kind


def sqlite_error(code: int | None) -> sqlite3.Error:
    error = sqlite3.OperationalError("failed")
    if code is not None:
        error.sqlite_errorcode = code
    return error


@pytest.mark.parametrize(
    ("code", "kind"),
    [
        (sqlite3.SQLITE_FULL, StorageErrorKind.DISK_FULL),
        (sqlite3.SQLITE_READONLY, StorageErrorKind.READ_ONLY),
        (sqlite3.SQLITE_PERM, StorageErrorKind.READ_ONLY),
        (sqlite3.SQLITE_IOERR_WRITE, StorageErrorKind.UNKNOWN),
        (sqlite3.SQLITE_BUSY, StorageErrorKind.LOCKED),
        (sqlite3.SQLITE_LOCKED, StorageErrorKind.LOCKED),
        (sqlite3.SQLITE_CANTOPEN, StorageErrorKind.MISSING),
        (sqlite3.SQLITE_READONLY_DBMOVED, StorageErrorKind.MISSING),
        (sqlite3.SQLITE_READONLY_ROLLBACK, StorageErrorKind.READ_ONLY),
        (sqlite3.SQLITE_CORRUPT, StorageErrorKind.UNKNOWN),
        (None, StorageErrorKind.UNKNOWN),
    ],
)
def test_sqlite_errors_are_told_apart_by_their_result_code(
    code: int | None, kind: StorageErrorKind
) -> None:
    assert error_kind(sqlite_error(code)) == kind


@pytest.mark.parametrize(
    ("number", "kind"),
    [
        (errno.ENOSPC, StorageErrorKind.DISK_FULL),
        (errno.EDQUOT, StorageErrorKind.DISK_FULL),
        (errno.EROFS, StorageErrorKind.READ_ONLY),
        (errno.EACCES, StorageErrorKind.READ_ONLY),
        (errno.EPERM, StorageErrorKind.READ_ONLY),
        (errno.ENOENT, StorageErrorKind.MISSING),
        (errno.EIO, StorageErrorKind.UNKNOWN),
    ],
)
def test_os_errors_are_told_apart_by_their_errno(number: int, kind: StorageErrorKind) -> None:
    assert error_kind(OSError(number, "failed")) == kind


def test_an_os_error_without_an_errno_is_not_recognised() -> None:
    assert error_kind(OSError("failed")) == StorageErrorKind.UNKNOWN
