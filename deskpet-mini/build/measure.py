"""测启动首帧时间与工作集：起进程 -> 等到自报首帧 -> 读内存 -> 截图 -> 关掉。"""
import ctypes, os, subprocess, sys, time
from ctypes import wintypes
from PIL import ImageGrab

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
LOG = os.path.join(ROOT, "build", "startup.log")
SHOT = os.path.join(ROOT, "build", "proof_small.png")

class PMC(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

def mem_mb(pid):
    h = ctypes.windll.kernel32.OpenProcess(0x0410, False, pid)
    pmc = PMC(); pmc.cb = ctypes.sizeof(PMC)
    ctypes.windll.psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb)
    ctypes.windll.kernel32.CloseHandle(h)
    return pmc.WorkingSetSize / 1e6, pmc.PeakWorkingSetSize / 1e6

width = sys.argv[1] if len(sys.argv) > 1 else "0"
if os.path.exists(LOG):
    os.remove(LOG)

args = [PY, os.path.join(ROOT, "pet.py"), "--log", LOG]
if width != "0":
    args += ["--width", width]

p = subprocess.Popen(args, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
t0 = time.monotonic()
ext = None
while time.monotonic() - t0 < 6:
    if os.path.exists(LOG):
        txt = open(LOG, encoding="utf-8").read()
        if "first_paint_ms" in txt:
            ext = (time.monotonic() - t0) * 1000
            break
    time.sleep(0.01)

print("外部测量 进程启动 -> 首帧日志: %.0f ms" % (ext or -1))
print(open(LOG, encoding="utf-8").read().strip())

time.sleep(7)          # 等缓存预热稳定 + 自报内存
print("--- pet 自报 ---")
print(open(LOG, encoding="utf-8").read().strip())
ws, peak = mem_mb(p.pid)
print("外部读到的工作集: %.0f MB (外部可能读到沙箱壳进程，仅参考)" % ws)
ImageGrab.grab().convert("RGB").save(SHOT)
print("截图 ->", SHOT)
p.terminate()
