"""Generate a simple PaperOpenClaw app icon (512x512 PNG)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from PIL import Image, ImageDraw  # noqa: E402


def main() -> int:
    out = Path(r"D:\PaperOpenClaw\desktop\icons\icon.png")
    out.parent.mkdir(parents=True, exist_ok=True)

    size = 512
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    # 圆角背景（深色）。
    draw.rounded_rectangle([16, 16, size - 16, size - 16], radius=96, fill=(18, 18, 24, 255))

    # 中心「书页」图案：三条渐宽横线，象征论文库。
    accent = (96, 165, 250, 255)  # 蓝
    draw.rounded_rectangle([112, 176, 400, 208], radius=16, fill=accent)
    draw.rounded_rectangle([112, 240, 400, 272], radius=16, fill=(148, 184, 240, 255))
    draw.rounded_rectangle([112, 304, 400, 336], radius=16, fill=(60, 70, 90, 255))

    image.save(out)
    print(f"图标已生成：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
