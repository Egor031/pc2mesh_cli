import ctypes
import os
from pathlib import Path
import shutil
import secrets
import stat
import sys


if os.name == "nt":
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _create_file = _kernel32.CreateFileW
    _create_file.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                            ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                            wintypes.HANDLE]
    _create_file.restype = wintypes.HANDLE
    _close_handle = _kernel32.CloseHandle
    _close_handle.argtypes = [wintypes.HANDLE]
    _close_handle.restype = wintypes.BOOL
    _move_file = _kernel32.MoveFileExW
    _move_file.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    _move_file.restype = wintypes.BOOL
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _get_security = _advapi32.GetKernelObjectSecurity
    _get_security.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p,
                             wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    _get_security.restype = wintypes.BOOL
    _get_control = _advapi32.GetSecurityDescriptorControl
    _get_control.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD)]
    _get_control.restype = wintypes.BOOL
    _set_security = _advapi32.SetFileSecurityW
    _set_security.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    _set_security.restype = wintypes.BOOL


def _resolve(path):
    path = Path(path)
    parts = path.parts[1:] if path.anchor else path.parts
    if (not path.name or "\0" in str(path)
            or (os.name == "nt" and (str(path).startswith("\\\\.\\")
                or any(":" in part or Path(part).is_reserved() for part in parts)))):
        raise ValueError("Expected a regular file path; devices and alternate streams are not supported.")
    return path.resolve()


def _stat(path):
    try:
        return Path(path).stat()
    except FileNotFoundError:
        return None


def _stamp(path):
    info = _stat(path)
    if info is None:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise OSError(f"Not a regular file: {path}")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def ensure_distinct_paths(paths):
    checked = []
    for label, path in paths.items():
        if path is None:
            continue
        resolved = os.path.normcase(str(_resolve(path)))
        info = _stat(path)
        for other_label, other_resolved, other_info in checked:
            if (resolved == other_resolved
                    or (info is not None and other_info is not None
                        and os.path.samestat(info, other_info))):
                raise ValueError(f"Path conflict: {label} and {other_label} refer to the same file.")
        checked.append((label, resolved, info))


class InputFileLock:
    """Deny writes/deletion of the open input on Windows, including through hard links."""

    def __init__(self, path):
        self.path = Path(path).absolute()
        _resolve(self.path)
        if os.name == "nt":
            import msvcrt

            handle = _create_file(str(self.path), 0x80000000, 1, None, 3, 0x80, None)
            if handle == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
            except Exception:
                _close_handle(handle)
                raise
            try:
                self.file = os.fdopen(descriptor, "rb")
            except Exception:
                os.close(descriptor)
                raise
        else:
            self.file = self.path.open("rb")
        self.identity = os.fstat(self.file.fileno())
        if not stat.S_ISREG(self.identity.st_mode):
            self.file.close()
            raise OSError(f"Input is not a regular file: {self.path}")

    def check(self):
        current = self.path.stat()
        if not os.path.samestat(self.identity, current):
            raise OSError("Input path changed while processing.")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.file.close()


def replace_file(source, target):
    if os.name == "nt":
        # No COPY_ALLOWED: publication must remain a same-volume rename.
        if not _move_file(str(source), str(target), 0x1 | 0x8):
            raise ctypes.WinError(ctypes.get_last_error())
    else:
        os.replace(source, target)


def _permissions(path):
    handle = _create_file(str(path), 0x10000 | 0x20000, 1 | 2 | 4, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        needed = wintypes.DWORD()
        if not _get_security(handle, 4, None, 0, ctypes.byref(needed)) and ctypes.get_last_error() != 122:
            raise ctypes.WinError(ctypes.get_last_error())
        descriptor = ctypes.create_string_buffer(needed.value)
        if not _get_security(handle, 4, descriptor, len(descriptor), ctypes.byref(needed)):
            raise ctypes.WinError(ctypes.get_last_error())
        control, revision = wintypes.WORD(), wintypes.DWORD()
        if not _get_control(descriptor, ctypes.byref(control), ctypes.byref(revision)):
            raise ctypes.WinError(ctypes.get_last_error())
        return descriptor, 4 | (0x80000000 if control.value & 0x1000 else 0x20000000)
    finally:
        _close_handle(handle)


def _preserve_permissions(source, target):
    if os.name == "nt" and _stat(source) is not None:
        descriptor, flags = _permissions(source)
        if not _set_security(str(target), flags, descriptor):
            raise ctypes.WinError(ctypes.get_last_error())


def _create_temporary(parent, suffix):
    # CPython 3.11 mkstemp retries PermissionError on Windows when os.access()
    # misses a directory ACL denial. Retry name collisions only, with a bound.
    flags = os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0)
    for attempt in range(32):
        path = parent / (".pc2mesh-" + secrets.token_hex(16) + suffix)
        try:
            return os.open(path, flags, 0o600), path
        except FileExistsError:
            continue
    raise FileExistsError("Could not create a unique temporary file.")


class AtomicOutput:
    def __init__(self, path, *, newline=None):
        self.requested = Path(path).absolute()
        self.target = _resolve(path)
        self.original = _stamp(self.target)
        self.path = None
        self.stream = None
        self.finished = False
        self.published = None
        descriptor, self.path = _create_temporary(self.target.parent, ".tmp")
        try:
            self.stream = os.fdopen(descriptor, "w", encoding="utf-8", newline=newline)
        except Exception:
            os.close(descriptor)
            self.path.unlink()
            raise

    def finish(self):
        if self.finished:
            return
        self.stream.flush()
        os.fsync(self.stream.fileno())
        self.stream.close()
        self.finished = True

    def publish(self):
        if not self.finished:
            raise OSError("Output must be flushed and closed before publication.")
        if (os.path.normcase(str(_resolve(self.requested))) != os.path.normcase(str(self.target))
                or _stamp(self.target) != self.original):
            raise OSError(f"Output changed while processing: {self.requested}")
        published = _stamp(self.path)
        _preserve_permissions(self.target, self.path)
        replace_file(self.path, self.target)
        self.path = None
        self.published = published

    def __enter__(self):
        return self

    def __exit__(self, *args):
        try:
            if self.stream is not None and not self.stream.closed:
                self.stream.close()
        finally:
            if self.path is not None:
                self.path.unlink(missing_ok=True)


def _backup(target):
    descriptor, path = _create_temporary(target.parent, ".bak")
    try:
        output = os.fdopen(descriptor, "wb")
    except Exception:
        os.close(descriptor)
        path.unlink(missing_ok=True)
        raise
    try:
        with output, target.open("rb") as source:
            shutil.copyfileobj(source, output)
            output.flush()
            os.fsync(output.fileno())
        _preserve_permissions(target, path)
        return path
    except Exception:
        path.unlink(missing_ok=True)
        raise


def publish_outputs(outputs, validate):
    """Roll back earlier publications if a later file (e.g. the log) cannot be published.

    This is not a filesystem transaction. A rollback blocked by another process
    retains the backup and reports its path rather than discarding the old data.
    """
    backups = {}
    published = []
    retained = set()
    try:
        validate()
        for output in outputs:
            output.finish()
        for output in outputs[:-1]:
            if output.original is not None:
                backups[output] = _backup(output.target)
        for output in outputs:
            validate()
            output.publish()
            published.append(output)
    except Exception as ex:
        rollback_errors = []
        for output in reversed(published):
            backup = backups.get(output)
            try:
                if _stamp(output.target) != output.published:
                    raise OSError("Published file changed before rollback.")
                if backup is None:
                    output.target.unlink()
                else:
                    replace_file(backup, output.target)
            except Exception as rollback_error:
                if backup is not None:
                    retained.add(backup)
                rollback_errors.append(f"{output.target}: {rollback_error}; backup: {backup}")
        if rollback_errors:
            raise OSError(f"{ex}; rollback failed: {'; '.join(rollback_errors)}") from ex
        raise
    finally:
        for backup in backups.values():
            if backup not in retained:
                try:
                    backup.unlink(missing_ok=True)
                except OSError as cleanup_error:
                    # Publications have already completed (or rolled back). A
                    # backup cleanup error must not misreport a committed log.
                    print(f"WARNING: Could not remove backup {backup}: {cleanup_error}", file=sys.stderr)
