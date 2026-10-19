"""跨进程文件互斥；锁文件身份保持固定，释放时不能删除再创建。"""

import os
from pathlib import Path


class FileLock:
    def __init__(self, path: Path):
        self.path = path
        self.stream = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        try:
            if os.name == "nt":
                import ctypes
                from ctypes import wintypes
                import msvcrt

                class Overlapped(ctypes.Structure):
                    _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                                ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                                ("hEvent", wintypes.HANDLE)]

                self.overlapped = Overlapped()
                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                self.lock_file = kernel.LockFileEx
                self.lock_file.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                          wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(Overlapped)]
                self.lock_file.restype = wintypes.BOOL
                self.unlock_file = kernel.UnlockFileEx
                self.unlock_file.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                            wintypes.DWORD, ctypes.POINTER(Overlapped)]
                self.unlock_file.restype = wintypes.BOOL
                self.handle = msvcrt.get_osfhandle(stream.fileno())
                # 同步句柄：由系统等待锁释放，不自行重试或设置等待次数。
                if not self.lock_file(self.handle, 2, 0, 1, 0, ctypes.byref(self.overlapped)):
                    raise ctypes.WinError(ctypes.get_last_error())
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        except BaseException:
            stream.close()
            raise
        self.stream = stream

    def release(self) -> None:
        stream = self.stream
        if stream is None:
            return
        try:
            if os.name == "nt":
                import ctypes

                if not self.unlock_file(self.handle, 0, 1, 0, ctypes.byref(self.overlapped)):
                    raise ctypes.WinError(ctypes.get_last_error())
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()
            self.stream = None
