"""The lab's media files report a real length and take no disk space, including on Windows."""

from __future__ import annotations

import ctypes
import os
import tempfile
from ctypes import wintypes

from tests.fakes.lab import write

GB = 2**30


def allocated(path: str) -> int:
    if os.name != "nt":
        return os.stat(path).st_blocks * 512
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCompressedFileSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetCompressedFileSizeW.restype = wintypes.DWORD
    high = wintypes.DWORD()
    low = kernel32.GetCompressedFileSizeW(path, ctypes.byref(high))
    if low == 0xFFFFFFFF and ctypes.get_last_error() != 0:
        raise OSError(ctypes.get_last_error(), path)
    return (high.value << 32) | low


def test_write_leaves_a_hole() -> None:
    with tempfile.TemporaryDirectory() as folder:
        src = write(os.path.join(folder, "movie.mkv"), GB)
        link = os.path.join(folder, "library.mkv")
        os.link(src, link)
        assert os.path.getsize(src) == GB
        assert os.path.getsize(link) == GB
        assert allocated(src) < 2**20
        assert allocated(link) < 2**20
