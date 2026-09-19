from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

root = Path(__file__).resolve().parents[1]
image = Image.new("RGB", (1200, 850), "#ffffff")
draw = ImageDraw.Draw(image)
font_path = "C:/Windows/Fonts/msyh.ttc"
title = ImageFont.truetype(font_path, 36)
body = ImageFont.truetype(font_path, 25)
small = ImageFont.truetype(font_path, 20)
draw.text((65, 55), "门店导视制作 / 测试样例", font=title, fill="#234c3c")
draw.text((65, 125), "铝牌 · 3mm · UV打印 · 数量 2 件", font=body, fill="#55685f")
draw.rectangle((250, 270, 950, 550), fill="#e6ebea", outline="#87968e", width=3)
draw.text((477, 360), "会议室", font=title, fill="#233c32")
draw.text((470, 420), "MEETING ROOM", font=body, fill="#506259")
draw.line((250, 220, 950, 220), fill="#788c81", width=2)
draw.line((250, 205, 250, 238), fill="#788c81", width=2)
draw.line((950, 205, 950, 238), fill="#788c81", width=2)
draw.text((546, 175), "300 mm", font=body, fill="#465e50")
draw.line((1000, 270, 1000, 550), fill="#788c81", width=2)
draw.text((1015, 390), "120 mm", font=small, fill="#465e50")
draw.text((65, 665), "宽 300mm × 高 120mm，每件尺寸相同", font=body, fill="#55685f")
draw.text((65, 715), "自动化测试资料，不是客户订单，不得用于生产报价。", font=small, fill="#9a7860")
target = root / ".runtime" / "phase2-fixture.png"
target.parent.mkdir(parents=True, exist_ok=True)
image.save(target)
print("Created local test fixture: .runtime/phase2-fixture.png")
