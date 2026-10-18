"""Generate original neutral car/link artwork, not an upstream service logo."""

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
image = Image.new("RGBA", (1024, 1024), (0, 0, 0, 0))
draw = ImageDraw.Draw(image)
draw.rounded_rectangle((48, 48, 976, 976), radius=200, fill="#153047")
# A simple car silhouette; two linked circles suggest remote connection.
draw.polygon([(240, 476), (328, 316), (696, 316), (784, 476)], fill="#57CFD0")
draw.polygon([(326, 464), (374, 372), (650, 372), (698, 464)], fill="#153047")
draw.rounded_rectangle((200, 456, 824, 704), radius=64, fill="#57CFD0")
for x in (268, 668):
    draw.rounded_rectangle((x, 654, x + 88, 770), radius=24, fill="#E7F5FA")
    draw.ellipse((x, 516, x + 88, 596), fill="#E7F5FA")
for x in (408, 496):
    draw.ellipse((x, 564, x + 120, 684), outline="#153047", width=22)
image = image.resize((256, 256), Image.Resampling.LANCZOS)
for relative in ("brand/icon.png", "custom_components/carlink/brand/icon.png"):
    path = ROOT / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="PNG", optimize=True)
