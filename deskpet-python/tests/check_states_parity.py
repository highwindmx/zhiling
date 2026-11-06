"""跨端 STATES 一致性校验：Python(state_map) ↔ Tauri 前端(shared.js)。

两个端各自维护一份 13 状态枚举，靠“成员一致、idle 结尾”保持行为对齐。
顺序不重要（优先级来自主题 JSON / DEFAULT_PRIORITY），因此按集合比对。

用法（仓库根目录）：
    .venv\\Scripts\\python.exe tests/check_states_parity.py
退出码 0 = 一致；1 = 不一致（并打印 diff）。

也可放进 CI（GitHub Actions / pre-commit）：
    python tests/check_states_parity.py
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import state_map as sm

# Tauri 前端 shared.js 的相对路径（写死相对仓库根，方便 CI 直接跑）
SHARED_JS = os.path.join(
    ROOT, "..", "deskpet-tauri", "frontend", "src", "shared.js"
)


def read_tauri_states(path):
    if not os.path.exists(path):
        # 仓库布局可能因部署不同；给个清晰报错，不盲目失败
        raise FileNotFoundError("找不到 Tauri 端 shared.js: %s" % path)
    text = open(path, encoding="utf-8").read()
    # 匹配 export const STATES = [ ... ]; 里的字符串字面量
    m = re.search(r"export const STATES\s*=\s*\[(.*?)\]", text, re.S)
    if not m:
        raise RuntimeError("shared.js 里没找到 STATES 数组")
    items = re.findall(r'"([a-z_0-9]+)"', m.group(1))
    return items


def main():
    py_states = list(sm.STATES)
    ta = read_tauri_states(os.path.normpath(SHARED_JS))
    if not ta:
        print("ERROR: Tauri STATES 解析为空")
        return 1

    py_set, ta_set = set(py_states), set(ta)
    ok = True
    if py_set != ta_set:
        ok = False
        missing_in_ta = sorted(py_set - ta_set)
        missing_in_py = sorted(ta_set - py_set)
        if missing_in_ta:
            print("Python 有但 Tauri 缺: %s" % ", ".join(missing_in_ta))
        if missing_in_py:
            print("Tauri 有但 Python 缺: %s" % ", ".join(missing_in_py))

    # idle 必须结尾（语义锚点）
    if py_states[-1] != "idle":
        ok = False
        print("Python STATES 未以 idle 结尾: %s" % py_states[-1])
    if ta[-1] != "idle":
        ok = False
        print("Tauri STATES 未以 idle 结尾: %s" % ta[-1])

    if ok:
        print("OK: 两端 STATES 一致（%d 个，均以 idle 结尾）" % len(py_states))
        return 0
    print("FAIL: 两端 STATES 不一致，请同步 v2_constants.STATES 与 shared.js STATES")
    return 1


if __name__ == "__main__":
    sys.exit(main())
