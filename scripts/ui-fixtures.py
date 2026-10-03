"""Generate customer-source fixtures; no business records or model requests."""
import io
from pathlib import Path

from openpyxl import Workbook
from openpyxl.drawing.image import Image as ExcelImage
from PIL import Image, ImageDraw

runtime = Path(__file__).resolve().parents[1] / ".runtime"
runtime.mkdir(exist_ok=True)
image = Image.new("RGB", (900, 600), "white")
draw = ImageDraw.Draw(image)
draw.rectangle((100, 100, 800, 400), outline="#358b62", width=5)
draw.text((110, 420), "300 x 120 mm", fill="#222222")
image.save(runtime / "phase2-fixture.png")
stream = io.BytesIO()
image.save(stream, "PNG")
stream.seek(0)
image.close()
book = Workbook()
sheet = book.active
sheet.title = "客户明细"
sheet.append(["序号", "产品名称", "类型备注", "工艺难度备注", "数量"])
for index in range(22):
    sheet.append([index + 1, f"项目{index + 1}", "发光字", "B", 2])
sheet.add_image(ExcelImage(stream), "F2")
book.save(runtime / "phase2-fixture.xlsx")
book.close()

many = Workbook()
sheet = many.active
sheet.title = "客户明细"
sheet.append(["序号", "产品名称", "类型备注", "工艺难度备注"])
sheet.append([1, "多图纸项目", "发光字", "B"])
image_data = (runtime / "phase2-fixture.png").read_bytes()
for _ in range(101):
    sheet.add_image(ExcelImage(io.BytesIO(image_data)), "F2")
many.save(runtime / "phase2-many-drawings.xlsx")
many.close()
