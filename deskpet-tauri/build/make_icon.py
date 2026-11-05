from PIL import Image, ImageDraw
import os

out_dir = r"D:\Share\Scripts\Explore\zhiling\deskpet-tauri\src-tauri\icons"
os.makedirs(out_dir, exist_ok=True)
size = 256
img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
# round body
d.ellipse([40, 30, 216, 226], fill=(99, 160, 255, 255))
d.ellipse([40, 30, 216, 226], outline=(40, 90, 200, 255), width=6)
# eyes
d.ellipse([88, 96, 116, 124], fill=(255, 255, 255, 255))
d.ellipse([140, 96, 168, 124], fill=(255, 255, 255, 255))
d.ellipse([98, 104, 110, 116], fill=(30, 40, 60, 255))
d.ellipse([150, 104, 162, 116], fill=(30, 40, 60, 255))
# smile
d.arc([92, 130, 164, 178], start=20, end=160, fill=(30, 40, 60, 255), width=6)
img.save(os.path.join(out_dir, "icon.png"))
img.save(os.path.join(out_dir, "icon.ico"))
# also 32x32 + 128x128 png for platform bundles
img.resize((32, 32)).save(os.path.join(out_dir, "32x32.png"))
img.resize((128, 128)).save(os.path.join(out_dir, "128x128.png"))
print("icons written:", os.listdir(out_dir))
