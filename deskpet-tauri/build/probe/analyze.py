from PIL import Image
from collections import Counter

for f in ["待机", "工作中", "不高兴", "真开心"]:
    im = Image.open(f + ".png").convert("RGB")
    w, h = im.size
    px = im.load()
    samples = [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1),
               (w // 2, 0), (0, h // 2), (w - 1, h // 2), (w // 2, h - 1)]
    cols = [px[x, y] for x, y in samples]
    c = Counter(cols)
    bg = list(c.most_common(1)[0][0])
    # count pixels "near" bg (any channel within 24)
    near = 0
    total = w * h
    stepx = max(1, w // 80)
    stepy = max(1, h // 80)
    for y in range(0, h, stepy):
        for x in range(0, w, stepx):
            r, g, b = px[x, y]
            if abs(r - bg[0]) < 24 and abs(g - bg[1]) < 24 and abs(b - bg[2]) < 24:
                near += 1
    sampled = ((w + stepx - 1) // stepx) * ((h + stepy - 1) // stepy)
    print(f"{f} {w}x{h} edge_bg={bg} approx_bg_ratio={near/sampled:.2f}")
