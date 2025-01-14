from contextlib import nullcontext
from functools import partial
from pathlib import Path
from typing import List, Optional

from rich.progress import Progress
from typer import Argument, Option

from belay import Device
from belay.cli.common import help_password, help_port


def sync_device(device, folder, progress_update, **kwargs):
    device.sync(folder, progress_update=progress_update, **kwargs)
    progress_update(description="Complete.")


def sync(
    port: str = Argument(..., help=help_port),
    folder: Path = Argument(..., help="Path of local file or folder to sync."),
    dst: str = Option("/", help="Destination directory to unpack folder contents to."),
    password: str = Option("", help=help_password),
    keep: Optional[List[str]] = Option(None, help="Files to keep."),
    ignore: Optional[List[str]] = Option(None, help="Files to ignore."),
    mpy_cross_binary: Optional[Path] = Option(
        None, help="Compile py files with this executable."
    ),
):
    """Synchronize a folder to device."""
    # Typer issues: https://github.com/tiangolo/typer/issues/410
    keep = keep if keep else None
    ignore = ignore if ignore else None

    with Device(port, password=password) as device, Progress() as progress:
        task_id = progress.add_task("")

        def progress_update(description=None, **kwargs):
            return progress.update(task_id, description=description, **kwargs)

        sync_device(
            device,
            folder,
            progress_update,
            dst=dst,
            keep=keep,
            ignore=ignore,
            mpy_cross_binary=mpy_cross_binary,
        )
