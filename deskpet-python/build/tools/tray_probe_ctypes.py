"""绕过 Qt，直接用 Shell_NotifyIconW 注册托盘图标：判断是 Qt 层失败还是系统层不接受。

用法：python build\\tools\\tray_probe_ctypes.py
10 秒后自动 NIM_DELETE 退出；日志同时写控制台和 build\\cache\\tray_probe_ctypes.log。
"""
import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOG_PATH = os.path.join(ROOT, "build", "cache", "tray_probe_ctypes.log")


def log(msg):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    if sys.stdout is not None:
        print(line, flush=True)


# ---------------- 环境信息 ----------------
def integrity_level(k32):
    """当前进程令牌的完整性级别 SID（Low=S-1-16-4096，Medium=S-1-16-8192）。"""
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    adv.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)]
    adv.OpenProcessToken.restype = wt.BOOL
    adv.GetTokenInformation.argtypes = [wt.HANDLE, ctypes.c_int, wt.LPVOID, wt.DWORD,
                                        ctypes.POINTER(wt.DWORD)]
    adv.GetTokenInformation.restype = wt.BOOL
    adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
    adv.ConvertSidToStringSidW.restype = wt.BOOL
    k32.OpenProcess.restype = wt.HANDLE
    h = k32.OpenProcess(0x1000, False, k32.GetCurrentProcessId())
    if not h:
        return "OpenProcess 失败 %d" % ctypes.get_last_error()
    ht = wt.HANDLE()
    if not adv.OpenProcessToken(h, 0x8, ctypes.byref(ht)):
        k32.CloseHandle(h)
        return "OpenProcessToken 失败 %d" % ctypes.get_last_error()
    n = wt.DWORD(0)
    adv.GetTokenInformation(ht, 25, None, 0, ctypes.byref(n))       # TokenIntegrityLevel
    buf = ctypes.create_string_buffer(n.value)
    adv.GetTokenInformation(ht, 25, buf, n.value, ctypes.byref(n))
    sidptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
    s = ctypes.c_wchar_p()
    out = s.value if adv.ConvertSidToStringSidW(ctypes.c_void_p(sidptr), ctypes.byref(s)) else "?"
    k32.CloseHandle(ht)
    k32.CloseHandle(h)
    return out


def env_info():
    log("python=%s" % sys.version.split()[0])
    try:
        from PyQt6.QtCore import QT_VERSION_STR, QSize
        from PyQt6.QtGui import QGuiApplication
        from PyQt6.QtWidgets import QApplication, QSystemTrayIcon
        app = QApplication.instance() or QApplication([])
        screen = app.primaryScreen()
        log("QT_VERSION_STR=%s platformName=%s screens=%d primary_dpi=%.2f logical_dpi=%.2f"
            % (QT_VERSION_STR, QGuiApplication.platformName(), len(app.screens()),
               screen.logicalDotsPerInch(), screen.physicalDotsPerInch()))
        log("isSystemTrayAvailable=%s supportsMessages=%s"
            % (QSystemTrayIcon.isSystemTrayAvailable(), QSystemTrayIcon.supportsMessages()))
    except Exception as e:
        log("Qt 环境读取失败：%r" % (e,))

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    sid = wt.DWORD()
    k32.ProcessIdToSessionId(k32.GetCurrentProcessId(), ctypes.byref(sid))
    log("windows_session_id=%d" % sid.value)

    # 完整性级别：Low(4096) 的进程向 Medium(8192) 的 explorer 注册托盘必然 ERROR_ACCESS_DENIED(5)
    log("integrity=%s（Low=4096 无法注册托盘；explorer 通常是 Medium=8192）" % integrity_level(k32))

    # 窗口站 / 桌面名：和 explorer 不在同一个 WinSta0\Default 时，Shell_NotifyIcon 会报 5
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    u32.GetProcessWindowStation.restype = wt.HANDLE
    u32.GetThreadDesktop.restype = wt.HANDLE
    u32.GetUserObjectInformationW.argtypes = [wt.HANDLE, ctypes.c_int, wt.LPVOID, wt.DWORD,
                                              ctypes.POINTER(wt.DWORD)]
    for tag, h in (("winsta", u32.GetProcessWindowStation()),
                   ("desktop", u32.GetThreadDesktop(k32.GetCurrentThreadId()))):
        buf = ctypes.create_unicode_buffer(256)
        need = wt.DWORD()
        ok = u32.GetUserObjectInformationW(h, 2, buf, ctypes.sizeof(buf), ctypes.byref(need))
        log("%s=%s ok=%s" % (tag, buf.value, bool(ok)))
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command",
                            "Get-Process explorer -ErrorAction SilentlyContinue | "
                            "Select-Object Id,SessionId | Format-Table -AutoSize | Out-String"],
                           capture_output=True, text=True, timeout=15, errors="ignore")
        log("explorer=%s" % " | ".join((r.stdout or "").split()))
    except Exception as e:
        log("读取 explorer 失败：%r" % (e,))


# ---------------- NOTIFYICONDATAW ----------------
class GUID(ctypes.Structure):
    _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD),
                ("Data4", wt.BYTE * 8)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", wt.UINT), ("uFlags", wt.UINT),
        ("uCallbackMessage", wt.UINT), ("hIcon", wt.HICON),
        ("szTip", wt.WCHAR * 128), ("dwState", wt.DWORD), ("dwStateMask", wt.DWORD),
        ("szInfo", wt.WCHAR * 256), ("uVersion", wt.UINT),
        ("szInfoTitle", wt.WCHAR * 64), ("dwInfoFlags", wt.DWORD),
        ("guidItem", GUID), ("hBalloonIcon", wt.HICON),
    ]


def main():
    env_info()
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    sh32 = ctypes.WinDLL("shell32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    u32.CreateWindowExW.restype = wt.HWND
    u32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                    wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
    ctypes.set_last_error(0)
    hwnd = u32.CreateWindowExW(0, "STATIC", "zhiling tray probe",
                               0x00CF0000, -2000, -2000, 1, 1,   # WS_OVERLAPPEDWINDOW，不 show
                               None, None, None, None)
    log("top_level_window=%s last_error=%s is_window=%s"
        % (hwnd, ctypes.get_last_error(), bool(u32.IsWindow(hwnd))))
    shell_tray = u32.FindWindowW("Shell_TrayWnd", None)
    log("Shell_TrayWnd=%s" % shell_tray)

    ico = os.path.join(ROOT, "assets", "icon.ico")
    IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE = 1, 0x10, 0x40
    u32.LoadImageW.restype = wt.HANDLE
    hicon = u32.LoadImageW(None, ico, IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE)
    log("LoadImageW(%s)=%s last_error=%s" % (ico, hicon, ctypes.get_last_error()))

    nid = NOTIFYICONDATAW()
    nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
    nid.hWnd = hwnd
    nid.uID = 0x5A11
    nid.uFlags = 0x1 | 0x2 | 0x4          # NIF_MESSAGE | NIF_ICON | NIF_TIP
    nid.uCallbackMessage = 0x0400 + 0x21  # WM_APP+33
    nid.hIcon = hicon
    nid.szTip = "智灵桌宠 ctypes 探针（10 秒）"

    sh32.Shell_NotifyIconW.argtypes = [wt.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
    sh32.Shell_NotifyIconW.restype = wt.BOOL

    def add(tag, hwnd_, hicon_, flags):
        n = NOTIFYICONDATAW()
        n.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        n.hWnd = hwnd_
        n.uID = 0x5A11
        n.uFlags = flags
        n.uCallbackMessage = 0x0400 + 0x21
        n.hIcon = hicon_
        n.szTip = "智灵桌宠 ctypes 探针（10 秒）"
        ctypes.set_last_error(0)
        r = sh32.Shell_NotifyIconW(0, ctypes.byref(n))
        log("NIM_ADD[%s] hwnd=%s hicon=%s flags=0x%x -> %s last_error=%s"
            % (tag, hwnd_, hicon_, flags, bool(r), ctypes.get_last_error()))
        return n if r else None

    NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x1, 0x2, 0x4
    u32.LoadIconW.restype = wt.HANDLE
    hicon_app = u32.LoadIconW(None, 32512)      # IDI_APPLICATION
    added = [x for x in (
        add("own_hwnd_own_icon", hwnd, hicon, NIF_MESSAGE | NIF_ICON | NIF_TIP),
        add("own_hwnd_app_icon", hwnd, hicon_app, NIF_MESSAGE | NIF_ICON | NIF_TIP),
        add("own_hwnd_no_msg", hwnd, hicon, NIF_ICON | NIF_TIP),
        add("null_hwnd", None, hicon, NIF_ICON | NIF_TIP),
    ) if x]
    log("added_count=%d" % len(added))
    if added:
        nid = added[0]
        ctypes.set_last_error(0)
        ok2 = sh32.Shell_NotifyIconW(1, ctypes.byref(nid))  # NIM_MODIFY
        log("NIM_MODIFY=%s last_error=%s" % (bool(ok2), ctypes.get_last_error()))

    msg = wt.MSG()
    end = time.time() + 10
    while time.time() < end:
        while u32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):   # PM_REMOVE
            u32.TranslateMessage(ctypes.byref(msg))
            u32.DispatchMessageW(ctypes.byref(msg))
        time.sleep(0.05)

    for n in added:
        ok3 = sh32.Shell_NotifyIconW(2, ctypes.byref(n))   # NIM_DELETE
        log("NIM_DELETE uid=%s -> %s" % (n.uID, bool(ok3)))
    if hicon:
        u32.DestroyIcon(hicon)
    if hwnd:
        u32.DestroyWindow(hwnd)
    log("done")


if __name__ == "__main__":
    main()
