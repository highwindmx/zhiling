# 把两张素材图以 base64 内联进 index.html，使 demo 完全自包含（可直接双击打开）。
# 用法：python build_demo.py
import base64, pathlib

root = pathlib.Path(__file__).resolve().parent
html = (root / "index.html").read_text(encoding="utf-8")

images = {
    "__FULL__": "670427261-d467fa11-8e3f-4bd1-adbf-28f1a4f14e8a.jpg",
    "__GRID__": "e2d07432c895d1431cbe792035f082025baf0766.jpg",
}

for token, fname in images.items():
    data = (root.parent / fname).read_bytes()
    b64 = base64.b64encode(data).decode("ascii")
    # 占位符替换为 data URI
    html = html.replace(token, "data:image/jpeg;base64," + b64)

(root / "index.html").write_text(html, encoding="utf-8")
print("done. index.html size =", (root / "index.html").stat().st_size, "bytes")
