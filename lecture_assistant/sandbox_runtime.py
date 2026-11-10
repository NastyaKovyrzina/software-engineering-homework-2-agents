"""Runtime checks for the trusted Sandboxie launcher and sandboxed process."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes


BOX_NAME = "LectureIsolationLab"


def get_sandbox_info() -> dict[str, object]:
    """Return Sandboxie status without loading its DLL into an unsandboxed process."""
    if os.name != "nt":
        return {"sandboxed": False, "box_name": None, "detection_error": "Windows is required."}

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        module = kernel32.GetModuleHandleW("SbieDll.dll")
        if not module:
            return {"sandboxed": False, "box_name": None, "detection_error": None}

        sbie = ctypes.WinDLL("SbieDll.dll", handle=module, use_last_error=True)
        query = sbie.SbieApi_QueryProcess
        query.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.LPWSTR,
                          wintypes.LPWSTR, ctypes.POINTER(wintypes.ULONG)]
        query.restype = wintypes.LONG
        box = ctypes.create_unicode_buffer(34)
        # Ненужные image_name и SID не запрашиваем: их буферы имеют другие размеры.
        status = query(wintypes.HANDLE(os.getpid()), box, None, None, None)
        if status < 0:
            return {"sandboxed": True, "box_name": None,
                    "detection_error": f"SbieApi_QueryProcess failed with status {status}."}
        return {"sandboxed": True, "box_name": box.value or None, "detection_error": None}
    except Exception as exc:
        # Once the injected DLL is present, uncertainty must not be treated as an escape.
        return {"sandboxed": True, "box_name": None,
                "detection_error": f"Sandboxie query failed: {type(exc).__name__}: {exc}"}
