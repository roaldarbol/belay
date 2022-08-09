import binascii
import hashlib
import json
import linecache
import tempfile
from abc import ABC, abstractmethod
from functools import wraps
from pathlib import Path
from typing import Callable, Dict, List, Optional, Union

from ._minify import minify as minify_code
from .inspect import getsource
from .pyboard import Pyboard, PyboardException

# Typing
JsonSerializeable = Union[None, bool, int, float, str, List, Dict]

# MicroPython Code Snippets
_BELAY_PREFIX = "__belay_"

_BELAY_STARTUP_CODE = f"""import ujson
def json_decorator(f):
    def belay_interface(*args, **kwargs):
        res = f(*args, **kwargs)
        print(ujson.dumps(res))
        return res
    globals()["{_BELAY_PREFIX}" + f.__name__] = belay_interface
    return f
"""

# Super common imports to speed up development
_DEFAULT_STARTUP_CODE = """
import binascii, errno, hashlib, machine, os, time
from machine import ADC, I2C, Pin, PWM, SPI, Timer
from time import sleep
from micropython import const
"""

_TRY_MKDIR_CODE = """import os; import errno
try:
    os.mkdir('%s')
except OSError as e:
    if e.errno != errno.EEXIST:
        raise
"""

# Creates and populates two set[str]: all_files, all_dirs
_BEGIN_SYNC_CODE = """import os, hashlib, binascii
all_files, all_dirs = set(), []
def enumerate_fs(path=""):
    for elem in os.ilistdir(path):
        full_name = path + "/" + elem[0]
        if elem[1] & 0x4000:  # is_dir
            all_dirs.append(full_name)
            enumerate_fs(full_name)
        else:
            all_files.add(full_name)
enumerate_fs()
all_dirs.sort()
del enumerate_fs
"""

_CLEANUP_SYNC_CODE = """
for file in all_files:
    os.remove(file)
for folder in reversed(all_dirs):
    try:
        os.rmdir(folder)
    except OSError:
        pass
del all_files, all_dirs
"""


class SpecialFilenameError(Exception):
    """Not allowed filename like ``boot.py`` or ``main.py``."""


class SpecialFunctionNameError(Exception):
    """Not allowed function name."""


def local_hash_file(fn):
    hasher = hashlib.sha256()
    with open(fn, "rb") as f:  # noqa: PL123
        while True:
            data = f.read(65536)
            if not data:
                break
            hasher.update(data)
    return binascii.hexlify(hasher.digest()).decode()


class Executer(ABC):
    def __init__(self, device):
        # To avoid Executer.__setattr__ raising an error
        object.__setattr__(self, "_belay_device", device)

    def __setattr__(self, name: str, value: Callable):
        if name.startswith("_belay") or (name.startswith("__") and name.endswith("__")):
            raise SpecialFunctionNameError(
                f'Not allowed to register function named "{name}".'
            )
        super().__setattr__(name, value)

    def __getattr__(self, name: str) -> Callable:
        # Just here for linting purposes.
        raise AttributeError

    @abstractmethod
    def __call__(self):
        raise NotImplementedError


class TaskExecuter(Executer):
    def __call__(
        self,
        f: Optional[Callable[..., JsonSerializeable]] = None,
        /,
        minify: bool = True,
    ) -> Callable[..., JsonSerializeable]:
        """Send code to device that executes when decorated function is called on-host.

        Parameters
        ----------
        f: Callable
            Function to decorate.
        minify: bool
            Minify ``cmd`` code prior to sending.

        Returns
        -------
        Callable
            Remote-executor function.
        """
        if f is None:
            return self  # type: ignore

        name = f.__name__
        src_code, src_lineno, src_file = getsource(f)

        # Add the json_decorator decorator for handling serialization.
        src_code = "@json_decorator\n" + src_code

        # Send the source code over to the device.
        self._belay_device(src_code, minify=minify)

        @wraps(f)
        def wrap(*args, **kwargs):
            cmd = f"{_BELAY_PREFIX + name}(*{args}, **{kwargs})"

            res = self._belay_device._traceback_execute(src_file, src_lineno, name, cmd)

            if hasattr(f, "_belay_level"):
                # Call next device's wrapper.
                if f._belay_level == 1:
                    res = [f(*args, **kwargs), res]
                else:
                    res = [*f(*args, **kwargs), res]

            return res

        wrap._belay_level = 1
        if hasattr(f, "_belay_level"):
            wrap._belay_level += f._belay_level

        setattr(self, name, wrap)

        return wrap


class ThreadExecuter(Executer):
    def __call__(
        self,
        f: Optional[Callable[..., None]] = None,
        minify: bool = True,
    ) -> Callable[..., None]:
        """Send code to device that spawns a thread when executed.

        Parameters
        ----------
        f: Callable
            Function to decorate.
        minify: bool
            Minify ``cmd`` code prior to sending.

        Returns
        -------
        Callable
            Remote-executor function.
        """
        if f is None:
            return self  # type: ignore

        name = f.__name__
        src_code, src_lineno, src_file = getsource(f)

        # Send the source code over to the device.
        self._belay_device(src_code, minify=minify)

        @wraps(f)
        def wrap(*args, **kwargs):
            cmd = f"import _thread; _thread.start_new_thread({name}, {args}, {kwargs})"
            self._belay_device._traceback_execute(src_file, src_lineno, name, cmd)

        setattr(self, name, wrap)

        return wrap


class Device:
    """Belay interface into a micropython device."""

    def __init__(
        self,
        *args,
        startup: str = _DEFAULT_STARTUP_CODE,
        **kwargs,
    ):
        """Create a MicroPython device.

        Parameters
        ----------
        startup: str
            Code to run on startup. Defaults to a few common imports.
        """
        self._board = Pyboard(*args, **kwargs)
        self._board.enter_raw_repl()

        self.task = TaskExecuter(self)
        self.thread = ThreadExecuter(self)

        self(_BELAY_STARTUP_CODE)
        if startup:
            self(startup)

    def __call__(
        self,
        cmd: str,
        deserialize: bool = True,
        minify: bool = True,
    ) -> JsonSerializeable:
        """Execute code on-device.

        Parameters
        ----------
        cmd: str
            Python code to execute.
        deserialize: bool
            Deserialize the received bytestream from device stdout as JSON data.
            Defaults to ``True``.
        minify: bool
            Minify ``cmd`` code prior to sending.
            Reduces the number of characters that need to be transmitted.
            Defaults to ``True``.

        Returns
        -------
            Return value from executing code on-device.
        """
        if minify:
            cmd = minify_code(cmd)

        res = self._board.exec(cmd).decode()

        if deserialize:
            if res:
                return json.loads(res)
            else:
                return None
        else:
            return res

    def sync(
        self,
        folder: Union[str, Path],
        minify=True,
    ) -> None:
        """Sync a local directory to the root of remote filesystem.

        For each local file, check the remote file's hash, and transfer if they differ.
        If a file/folder exists on the remote filesystem that doesn't exist in the local
        folder, then delete it.

        Parameters
        ----------
        folder: str, Path
            Directory of files to sync to the root of the board's filesystem.
        """
        folder = Path(folder)

        if not folder.exists():
            raise ValueError(f"{dir} does not exist")
        if not folder.is_dir():
            raise ValueError(f"{dir} is not a directory.")

        # Create a list of all files and dirs (on-device).
        # This is so we know what to clean up after done syncing.
        self(_BEGIN_SYNC_CODE)

        @self.task
        def remote_hash_file(fn):
            hasher = hashlib.sha256()
            try:
                with open(fn, "rb") as f:  # noqa: PL123
                    while True:
                        data = f.read(4096)
                        if not data:
                            break
                        hasher.update(data)
            except OSError:
                return "0" * 64
            return str(binascii.hexlify(hasher.digest()))

        # Sort so that folder creation comes before file sending.
        local_files = sorted(folder.rglob("*"))
        for src in local_files:
            dst = f"/{src.relative_to(folder)}"

            if dst in {"boot.py", "main.py"}:
                raise SpecialFilenameError(
                    f"Cannot upload {dst}, would interfere with REPL."
                )

            with tempfile.TemporaryDirectory() as tmp_dir:
                tmp_dir = Path(tmp_dir)  # Used if we need to perform a conversion

                if src.is_dir():
                    self(_TRY_MKDIR_CODE % dst)
                    continue

                if minify and src.suffix == ".py":
                    minified = minify_code(src.read_text())
                    src = tmp_dir / src.name
                    src.write_text(minified)

                # All other files, just sync over.
                local_hash = local_hash_file(src)
                remote_hash = remote_hash_file(dst)
                if local_hash != remote_hash:
                    self._board.fs_put(src, dst)
                self(f'all_files.discard("{dst}")')

        # Remove all the files and directories that did not exist in local filesystem.
        self(_CLEANUP_SYNC_CODE)

    def _traceback_execute(
        self,
        src_file: Union[str, Path],
        src_lineno: int,
        name: str,
        cmd: str,
    ):
        """Invoke ``cmd``, and reinterprets raised stacktrace in ``PyboardException``.

        Parameters
        ----------
        src_file: Union[str, Path]
            Path to the file containing the code of the function that ``cmd`` will execute.
        src_lineno: int
            Line number into ``src_file`` that the function starts.
        name: str
            Name of the function.
        cmd: str
            Python command that executes a function on-device.
        """
        src_file = str(src_file)

        try:
            res = self(cmd)
        except PyboardException as e:
            new_lines = []

            msg = e.args[0]
            lines = msg.split("\n")
            for line in lines:
                new_lines.append(line)

                try:
                    file, lineno, fn = line.strip().split(",", 2)
                except ValueError:
                    continue

                if file != 'File "<stdin>"' or fn != f" in {name}":
                    continue

                lineno = int(lineno[6:]) - 1 + src_lineno

                new_lines[-1] = f'  File "{src_file}", line {lineno},{fn}'

                # Get what that line actually is.
                new_lines.append("    " + linecache.getline(src_file, lineno).strip())
            new_msg = "\n".join(new_lines)
            e.args = (new_msg,)
            raise
        return res
