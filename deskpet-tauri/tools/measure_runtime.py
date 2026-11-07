#!/usr/bin/env python3
"""测量某个进程及其整棵子进程树的运行期资源占用（Windows，纯标准库）。

Tauri 版会拉起一组 msedgewebview2 子进程，只量主进程会严重低估；
本脚本按 ParentProcessId 递归收集整棵树，输出工作集 / 私有提交 / CPU。

用法：
    python tools/measure_runtime.py --launch path\\to\\app.exe --name app --samples 12
    python tools/measure_runtime.py --name ZhiLing --samples 12
    python tools/measure_runtime.py --pid 12345 --samples 12 --interval 0.5
    python tools/measure_runtime.py --launch ... --json out.json

指标口径：
    WS      工作集（WorkingSetSize），进程树合计，MB = 字节 / 1e6
    PRIVATE 私有提交（PagefileUsage），不含可共享的 DLL
    CPU%    (内核态+用户态 CPU 时间增量) / 墙钟时间 * 100，单核 100% 为满
"""
import argparse
import ctypes
import ctypes.wintypes as w
import json
import os
import time

TH32CS_SNAPPROCESS = 0x00000002
MAX_PATH = 260
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
PROCESS_TERMINATE = 0x0001

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
psapi = ctypes.WinDLL("psapi", use_last_error=True)


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", w.DWORD),
        ("cntUsage", w.DWORD),
        ("th32ProcessID", w.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", w.DWORD),
        ("cntThreads", w.DWORD),
        ("th32ParentProcessID", w.DWORD),
        ("pcPriClassBase", w.LONG),
        ("dwFlags", w.DWORD),
        ("szExeFile", w.WCHAR * MAX_PATH),
    ]


class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", w.DWORD),
        ("PageFaultCount", w.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", w.DWORD), ("dwHighDateTime", w.DWORD)]


k32.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
k32.CreateToolhelp32Snapshot.restype = w.HANDLE
k32.Process32FirstW.argtypes = [w.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
k32.Process32FirstW.restype = w.BOOL
k32.Process32NextW.argtypes = [w.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
k32.Process32NextW.restype = w.BOOL
k32.CloseHandle.argtypes = [w.HANDLE]
k32.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
k32.OpenProcess.restype = w.HANDLE
k32.GetProcessTimes.argtypes = [w.HANDLE] + [ctypes.POINTER(FILETIME)] * 4
k32.GetProcessTimes.restype = w.BOOL
k32.TerminateProcess.argtypes = [w.HANDLE, w.UINT]
k32.TerminateProcess.restype = w.BOOL
psapi.GetProcessMemoryInfo.argtypes = [
    w.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS), w.DWORD]
psapi.GetProcessMemoryInfo.restype = w.BOOL


def ft_int(ft):
    return (ft.dwHighDateTime << 32) | ft.dwLowDateTime


def list_processes():
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(entry)
    out = []
    if k32.Process32FirstW(snap, ctypes.byref(entry)):
        while True:
            out.append((entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile))
            if not k32.Process32NextW(snap, ctypes.byref(entry)):
                break
    k32.CloseHandle(snap)
    return out


def process_tree(root_pid, procs=None):
    procs = procs if procs is not None else list_processes()
    names = {pid: name for pid, _, name in procs}
    children = {}
    for pid, ppid, _ in procs:
        children.setdefault(ppid, []).append(pid)
    seen, stack, order = set(), [root_pid], []
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        order.append((pid, names.get(pid, "?")))
        stack.extend(children.get(pid, []))
    return order


def read_stat(pid):
    handle = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not handle:
        return None
    try:
        pmc = PROCESS_MEMORY_COUNTERS()
        pmc.cb = ctypes.sizeof(pmc)
        if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
            return None
        ft = [FILETIME() for _ in range(4)]
        if not k32.GetProcessTimes(handle, *[ctypes.byref(x) for x in ft]):
            return None
        cpu_ns = (ft_int(ft[2]) + ft_int(ft[3])) * 100
        return {"ws": pmc.WorkingSetSize, "private": pmc.PagefileUsage, "cpu_ns": cpu_ns}
    finally:
        k32.CloseHandle(handle)


def terminate_tree(root_pid):
    """只回收自己启动的进程树，不动任何外部进程。"""
    pids = [root_pid] + [pid for pid, _ in process_tree(root_pid) if pid != root_pid]
    for pid in reversed(pids):
        handle = k32.OpenProcess(PROCESS_TERMINATE, False, pid)
        if handle:
            k32.TerminateProcess(handle, 0)
            k32.CloseHandle(handle)


def find_pid_by_name(name):
    hits = [p for p in list_processes() if p[2].lower() == name.lower()
            or p[2].lower() == name.lower() + ".exe"]
    if not hits:
        raise SystemExit(f"找不到进程：{name}")
    return hits[0][0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", help="进程名（如 ZhiLing / pythonw）")
    ap.add_argument("--pid", type=int, help="直接给主进程 PID")
    ap.add_argument("--launch", default="", help="由本脚本启动目标程序并回收")
    ap.add_argument("--cwd", default="", help="--launch 时的工作目录")
    ap.add_argument("--launch-args", nargs="*", default=[], help="--launch 时的附加参数")
    ap.add_argument("--env", action="append", default=[], metavar="KEY=VALUE",
                    help="--launch 时注入/覆盖环境变量，可重复")
    ap.add_argument("--warmup", type=float, default=6.0, help="启动后预热秒数")
    ap.add_argument("--samples", type=int, default=12, help="采样次数")
    ap.add_argument("--interval", type=float, default=0.5, help="采样间隔（秒）")
    ap.add_argument("--json", default="", help="可选，把明细写入 JSON")
    args = ap.parse_args()

    proc = None
    if args.launch:
        import subprocess
        env = dict(os.environ)
        for item in args.env:
            if "=" in item:
                key, value = item.split("=", 1)
                env[key] = value
        proc = subprocess.Popen([args.launch] + args.launch_args,
                                cwd=args.cwd or None, env=env)
        root = proc.pid
        print(f"# launched pid={root} {args.launch} {' '.join(args.launch_args)}")
        time.sleep(args.warmup)
        if proc.poll() is not None:
            raise SystemExit(f"目标程序启动后立刻退出，code={proc.returncode}（详见程序自身日志）")
    else:
        root = args.pid or find_pid_by_name(args.name)
    label = args.name or f"pid={root}"
    prev = {}
    rows = []
    print(f"# target={label} root_pid={root} samples={args.samples} interval={args.interval}s")
    print(f"{'t(s)':>6} {'proc':>4} {'WS(MB)':>9} {'PRIV(MB)':>9} {'CPU%':>7}")
    for i in range(args.samples):
        t0 = time.perf_counter()
        time.sleep(args.interval)
        elapsed = time.perf_counter() - t0
        tree = process_tree(root)
        cur = {}
        for pid, _ in tree:
            s = read_stat(pid)
            if s:
                cur[pid] = s
        ws = sum(s["ws"] for s in cur.values()) / 1e6
        priv = sum(s["private"] for s in cur.values()) / 1e6
        cpu = sum(max(0.0, s["cpu_ns"] - prev.get(pid, s)["cpu_ns"])
                  for pid, s in cur.items()) / (elapsed * 1e9) * 100
        prev = cur
        rows.append({"t": round((i + 1) * args.interval, 2), "procs": len(cur),
                     "ws_mb": round(ws, 2), "private_mb": round(priv, 2),
                     "cpu_pct": round(cpu, 2)})
        print(f"{rows[-1]['t']:6.1f} {len(cur):4d} {ws:9.2f} {priv:9.2f} {cpu:7.2f}")

    stable = rows[len(rows) // 3:] or rows
    summary = {
        "target": label,
        "root_pid": root,
        "ws_avg_mb": round(sum(r["ws_mb"] for r in stable) / len(stable), 2),
        "ws_peak_mb": max(r["ws_mb"] for r in rows),
        "private_avg_mb": round(sum(r["private_mb"] for r in stable) / len(stable), 2),
        "cpu_avg_pct": round(sum(r["cpu_pct"] for r in stable) / len(stable), 2),
        "cpu_peak_pct": max(r["cpu_pct"] for r in rows),
        "proc_count": rows[-1]["procs"],
        "tree": [{"pid": pid, "name": name} for pid, name in process_tree(root)],
    }
    print("\n# summary " + json.dumps(summary, ensure_ascii=False))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"samples": rows, "summary": summary}, f, ensure_ascii=False, indent=2)
        print(f"# wrote {args.json}")
    if proc:
        terminate_tree(root)
        print(f"# terminated pid={root} and its child processes")


if __name__ == "__main__":
    main()
