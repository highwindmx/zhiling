"""同口径对比 deskpet-tauri(Tauri/WebView2) 与 deskpet-mini(PyQt6) 的体积、内存、CPU、首帧。

只用标准库 + ctypes 读 Win32 计数，避免额外依赖：
  * 体积：文件/目录字节数（分"仅程序本体"与"含运行时依赖"两列）
  * 内存：按进程树递归统计 WorkingSetSize，1s 采样 60s，给均值/峰值；WebView2 子进程单列
  * CPU ：同一段采样里 GetProcessTimes 的增量 / 墙钟时间，折算单核百分比
  * 首帧：各程序自带的进程内计时（Tauri first_show_ms / mini first_paint_ms）
           + 外部观测"进程创建→可见窗口"（EnumWindows 轮询 20ms）
  * 稳定性：结束时进程是否存活、窗口是否仍可见，并抓一张桌面截图

用法:
    python build/bench.py                    # 两版各跑一次，60s
    python build/bench.py --seconds 60 --only tauri
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import glob
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
ROOT = os.path.dirname(PROJECT)
MINI = os.path.join(ROOT, "deskpet-mini")
SHOTS = os.path.join(PROJECT, "docs", "screenshots")
TMP = os.environ.get("TEMP", r"C:\Users\KaishengMao\AppData\Local\Temp")
LOGS = os.path.join(HERE, "bench-logs")
TAURI_LOG = os.path.join(LOGS, "tauri.log")
MINI_LOG = os.path.join(LOGS, "mini.log")

WEBVIEW2_RUNTIME = r"C:\Program Files (x86)\Microsoft\EdgeWebView\Application"

# ---- Win32 ---------------------------------------------------------------
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)
psapi = ctypes.WinDLL("psapi", use_last_error=True)

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
ACCESS = PROCESS_QUERY_INFORMATION | PROCESS_VM_READ


class PMC_EX(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)), ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wt.DWORD), ("dwHighDateTime", wt.DWORD)]


def _ticks(ft):
    return (ft.dwHighDateTime << 32) | ft.dwLowDateTime


def process_table():
    """pid -> (parent_pid, exe_name)"""
    snap = k32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if snap == -1:
        return {}
    out = {}
    e = PROCESSENTRY32W()
    e.dwSize = ctypes.sizeof(e)
    ok = k32.Process32FirstW(snap, ctypes.byref(e))
    while ok:
        out[e.th32ProcessID] = (e.th32ParentProcessID, e.szExeFile)
        ok = k32.Process32NextW(snap, ctypes.byref(e))
    k32.CloseHandle(snap)
    return out


def tree_pids(root):
    table = process_table()
    children = {}
    for pid, (ppid, _) in table.items():
        children.setdefault(ppid, []).append(pid)
    seen, stack = set(), [root]
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        if pid in table:
            stack.extend(children.get(pid, []))
    return {p: table.get(p, (0, "?"))[1] for p in seen}


def mem_cpu(pid):
    """返回 (WorkingSetSize 字节, CPU 秒)，取不到返回 None"""
    h = k32.OpenProcess(ACCESS, False, pid)
    if not h:
        return None
    try:
        pmc = PMC_EX()
        pmc.cb = ctypes.sizeof(PMC_EX)
        if not psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
            return None
        c, e, k, u = FILETIME(), FILETIME(), FILETIME(), FILETIME()
        if not k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e),
                                   ctypes.byref(k), ctypes.byref(u)):
            return None
        cpu = (_ticks(k) + _ticks(u)) / 1e7
        return pmc.WorkingSetSize, cpu
    finally:
        k32.CloseHandle(h)


def visible_windows():
    """[(pid, rect)]，只取可见且 >=100px 的顶层窗口"""
    found = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        r = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        if r.right - r.left >= 100 and r.bottom - r.top >= 100:
            found.append((pid.value, (r.left, r.top, r.right - r.left, r.bottom - r.top)))
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return found


def dir_size(path):
    total = 0
    for base, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(base, f))
            except OSError:
                pass
    return total


def tree_size(path):
    if os.path.isfile(path):
        return os.path.getsize(path)
    return dir_size(path)


def find_tauri_exe():
    cands = glob.glob(os.path.join(PROJECT, "src-tauri", "target", "release", "deskpet-tauri.exe"))
    cands += glob.glob(os.path.join(TMP, "deskpet-tauri-target", "release", "deskpet-tauri.exe"))
    if not cands:
        raise SystemExit("找不到 release 版 deskpet-tauri.exe，先 cargo build --release")
    return max(cands, key=os.path.getmtime)


def find_installer():
    pats = [
        os.path.join(PROJECT, "src-tauri", "target", "release", "bundle", "nsis", "*.exe"),
        os.path.join(TMP, "deskpet-tauri-target", "release", "bundle", "nsis", "*.exe"),
        os.path.join(PROJECT, "src-tauri", "target", "release", "bundle", "msi", "*.msi"),
        os.path.join(TMP, "deskpet-tauri-target", "release", "bundle", "msi", "*.msi"),
    ]
    files = []
    for p in pats:
        files += glob.glob(p)
    return max(files, key=os.path.getmtime) if files else None


# ---- 测量 -----------------------------------------------------------------
def wait_for_line(path, key, timeout=20.0, t0=None):
    """轮询日志文件里的关键行，返回 (行文本, 从 t0 起的毫秒)"""
    deadline = time.time() + timeout
    start = t0 or time.time()
    while time.time() < deadline:
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if line.startswith(key):
                        return line.strip(), (time.time() - start) * 1000
        except FileNotFoundError:
            pass
        time.sleep(0.02)
    return None, None


def wait_visible(root_pid, timeout=20.0, t0=None):
    """进程树里出现可见窗口的毫秒数"""
    start = t0 or time.time()
    deadline = start + timeout
    while time.time() < deadline:
        tree = set(tree_pids(root_pid))
        hits = [(p, r) for p, r in visible_windows() if p in tree]
        if hits:
            return (time.time() - start) * 1000, hits[0][1]
        time.sleep(0.02)
    return None, None


def capture(rect, path):
    x, y, w, h = rect
    os.makedirs(os.path.dirname(path), exist_ok=True)
    ps = f"""
Add-Type -AssemblyName System.Windows.Forms,System.Drawing
Add-Type @'
using System;using System.Runtime.InteropServices;
public class D {{ [DllImport("user32.dll")] public static extern bool SetProcessDPIAware(); }}
'@
[D]::SetProcessDPIAware() | Out-Null
$bmp = New-Object System.Drawing.Bitmap({w},{h})
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen({x},{y},0,0,(New-Object System.Drawing.Size({w},{h})))
$bmp.Save('{path}', [System.Drawing.Imaging.ImageFormat]::Png)
"""
    subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                   capture_output=True, timeout=60)


def sample(root_pid, seconds, shot_path=None, shot_at=30):
    """采样内存/CPU；返回原始样本"""
    samples = []
    t0 = time.time()
    nxt = t0
    while time.time() - t0 < seconds:
        tree = tree_pids(root_pid)
        rec = {}
        for pid in tree:
            mc = mem_cpu(pid)
            if mc:
                rec[pid] = mc
        samples.append({"t": time.time() - t0, "procs": rec})
        if shot_path and time.time() - t0 >= shot_at:
            hits = [(p, r) for p, r in visible_windows() if p in tree]
            if hits:
                capture(hits[0][1], shot_path)
            shot_path = None
        nxt += 1.0
        time.sleep(max(0, nxt - time.time()))
    return samples


def summarize(samples):
    wss = [sum(v[0] for v in s["procs"].values()) / 1e6 for s in samples]
    first = samples[0]["procs"]
    last = samples[-1]["procs"]
    cpu_first = sum(v[1] for v in first.values())
    cpu_last = sum(v[1] for v in last.values())
    wall = samples[-1]["t"] - samples[0]["t"]
    cpu_pct = (cpu_last - cpu_first) / wall * 100 if wall > 0 else 0
    return {
        "wss_mean_mb": round(sum(wss) / len(wss), 1),
        "wss_max_mb": round(max(wss), 1),
        "wss_last_mb": round(wss[-1], 1),
        "cpu_pct": round(cpu_pct, 2),
        "proc_count": len(last),
    }


def role_of(name, cmdline):
    if not name.lower().startswith("msedgewebview2"):
        return "主进程"
    if "--type=" in (cmdline or ""):
        t = (cmdline.split("--type=")[1].split()[0]).strip('"')
        return f"WebView2 {t}"
    return "WebView2 browser"


def cmdlines(pids):
    """一次性取命令行走 PowerShell（只在结束时调一次）"""
    if not pids:
        return {}
    flt = " or ".join(f"ProcessId={p}" for p in pids)
    ps = (f"Get-CimInstance Win32_Process -Filter \"{flt}\" | "
          "Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                         capture_output=True, text=True, timeout=60)
    try:
        data = json.loads(out.stdout or "[]")
    except json.JSONDecodeError:
        return {}
    if isinstance(data, dict):
        data = [data]
    return {int(d["ProcessId"]): (d.get("Name") or "", d.get("CommandLine") or "") for d in data}


def breakdown(samples, roles):
    """按角色聚合：先把同角色的多个进程在每个采样点求和，再算均值/峰值"""
    acc = {}
    for s in samples:
        by_role = {}
        for pid, (ws, _cpu) in s["procs"].items():
            r = roles.get(pid, "其他")
            by_role[r] = by_role.get(r, 0) + ws
        for r, v in by_role.items():
            acc.setdefault(r, []).append(v / 1e6)
    return {k: {"mean_mb": round(sum(v) / len(v), 1), "peak_mb": round(max(v), 1),
                "samples": len(v)} for k, v in sorted(acc.items())}


# ---- 两个被测目标 ---------------------------------------------------------
def bench_tauri(seconds):
    exe = find_tauri_exe()
    for p in ("deskpet-tauri.exe",):
        subprocess.run(["taskkill", "/IM", p, "/F"], capture_output=True)
    time.sleep(0.3)
    if os.path.exists(TAURI_LOG):
        os.remove(TAURI_LOG)
    env = dict(os.environ)
    env["DESKPET_LOG"] = TAURI_LOG
    # 素材外置：exe 在 %TEMP%，素材在 deskpet-tauri\assets，用环境变量指过去
    env.setdefault("DESKPET_ASSETS", os.path.join(PROJECT, "assets"))
    t_spawn = time.time()
    proc = subprocess.Popen([exe], env=env)
    first_line, first_self = wait_for_line(TAURI_LOG, "first_show_ms", t0=t_spawn)
    first_vis, rect = wait_visible(proc.pid, t0=t_spawn)
    samples = sample(proc.pid, seconds, os.path.join(SHOTS, "bench-tauri-idle.png"))
    pids = set(samples[-1]["procs"])
    roles = {p: role_of(n, c) for p, (n, c) in cmdlines(pids).items()}
    res = summarize(samples)
    res["first_show_ms"] = float(first_line.split("=")[1].split()[0]) if first_line else None
    res["first_visible_ms"] = round(first_vis) if first_vis else None
    res["alive"] = proc.poll() is None
    res["roles"] = breakdown(samples, roles)
    res["exe"] = exe
    res["exe_mb"] = round(os.path.getsize(exe) / 1e6, 1)
    inst = find_installer()
    res["installer"] = os.path.basename(inst) if inst else None
    res["installer_mb"] = round(os.path.getsize(inst) / 1e6, 1) if inst else None
    if rect:
        res["rect"] = rect
    proc.terminate()
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        proc.kill()
    subprocess.run(["taskkill", "/IM", "deskpet-tauri.exe", "/F"], capture_output=True)
    return res


def bench_mini(seconds):
    py = os.path.join(MINI, ".venv", "Scripts", "pythonw.exe")
    if not os.path.exists(py):
        py = os.path.join(MINI, ".venv", "Scripts", "python.exe")
    if not os.path.exists(py):
        py = sys.executable
    if os.path.exists(MINI_LOG):
        os.remove(MINI_LOG)
    t_spawn = time.time()
    proc = subprocess.Popen([py, "pet.py", "--log", MINI_LOG], cwd=MINI)
    first_line, _ = wait_for_line(MINI_LOG, "first_paint_ms", t0=t_spawn)
    first_vis, rect = wait_visible(proc.pid, t0=t_spawn)
    samples = sample(proc.pid, seconds, os.path.join(SHOTS, "bench-mini-idle.png"))
    pids = set(samples[-1]["procs"])
    roles = {p: role_of(n, c) for p, (n, c) in cmdlines(pids).items()}
    res = summarize(samples)
    res["first_paint_ms"] = float(first_line.split("=")[1].split()[0]) if first_line else None
    res["first_visible_ms"] = round(first_vis) if first_vis else None
    res["alive"] = proc.poll() is None
    res["roles"] = breakdown(samples, roles)
    if rect:
        res["rect"] = rect
    proc.terminate()
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        proc.kill()
    return res


def sizes():
    out = {}
    exe = find_tauri_exe()
    out["tauri_exe_mb"] = round(os.path.getsize(exe) / 1e6, 1)
    inst = find_installer()
    out["tauri_installer"] = os.path.basename(inst) if inst else None
    out["tauri_installer_mb"] = round(os.path.getsize(inst) / 1e6, 1) if inst else None
    wv = sorted(glob.glob(os.path.join(WEBVIEW2_RUNTIME, "*")))
    wv = [p for p in wv if os.path.isdir(p) and os.path.basename(p)[0].isdigit()]
    out["webview2_mb"] = round(dir_size(wv[-1]) / 1e6, 1) if wv else None

    assets = os.path.join(PROJECT, "assets")
    out["tauri_assets_mb"] = round(dir_size(assets) / 1e6, 1) if os.path.isdir(assets) else 0.0
    out["tauri_total_mb"] = round(out["tauri_exe_mb"] + out["tauri_assets_mb"], 1)

    own = [os.path.join(MINI, f) for f in
           ("pet.py", "clips.json", "meta.json", "requirements.txt")]
    own += [os.path.join(MINI, d) for d in
            ("frames_idle", "frames_action", "frames_action2", "frames_action3")]
    out["mini_own_mb"] = round(sum(tree_size(p) for p in own) / 1e6, 1)
    out["mini_frames_all_mb"] = round(sum(
        tree_size(p) for p in glob.glob(os.path.join(MINI, "frames_*"))) / 1e6, 1)
    qt = 0
    for p in glob.glob(os.path.join(MINI, ".venv", "Lib", "site-packages", "*")):
        b = os.path.basename(p).lower()
        if b.startswith("pyqt6") or b.startswith("pyqt6_") or b == "pyqt6_sip":
            qt += tree_size(p)
    out["mini_pyqt_mb"] = round(qt / 1e6, 1)
    out["mini_venv_mb"] = round(dir_size(os.path.join(MINI, ".venv")) / 1e6, 1)
    out["mini_with_pyqt_mb"] = round((out["mini_own_mb"] * 1e6 + qt) / 1e6, 1)
    out["mini_with_venv_mb"] = round((out["mini_own_mb"] * 1e6
                                      + dir_size(os.path.join(MINI, ".venv"))) / 1e6, 1)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=60)
    ap.add_argument("--only", choices=["tauri", "mini"], default=None)
    args = ap.parse_args()
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass
    os.makedirs(SHOTS, exist_ok=True)
    os.makedirs(LOGS, exist_ok=True)

    result = {"seconds": args.seconds, "sizes": sizes()}
    if args.only in (None, "tauri"):
        print("measuring tauri ...", flush=True)
        result["tauri"] = bench_tauri(args.seconds)
    if args.only in (None, "mini"):
        print("measuring mini ...", flush=True)
        result["mini"] = bench_mini(args.seconds)

    out_json = os.path.join(HERE, "bench-result.json")
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("wrote", out_json)


if __name__ == "__main__":
    main()
