import hashlib
import io
import math
import threading
import uuid
import warnings
from contextlib import closing
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image, ImageOps, UnidentifiedImageError

from .config import settings

# PDFium is not thread-safe. Rendering is bounded and serialized in this process.
PDF_LOCK = threading.Lock()
Image.MAX_IMAGE_PIXELS = 25_000_000
FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}


class FileRejected(ValueError):
    pass


def storage_path(key: str) -> Path:
    root = settings().storage_dir.resolve()
    path = (root / key).resolve()
    if not path.is_relative_to(root) or path == root:
        raise FileRejected("无效文件路径")
    return path


def pdf_page_text(file, page_number):
    from .customer_excel import MEDIA_TYPE, page_source
    if getattr(file, "media_type", "") == MEDIA_TYPE:
        import json
        return json.dumps(page_source(storage_path(file.storage_key), page_number), ensure_ascii=False)
    if getattr(file, "media_type", "") != "application/pdf":
        return ""
    with PDF_LOCK:
        doc = pdfium.PdfDocument(storage_path(file.storage_key) / "original")
        try:
            page = doc[page_number - 1]
            try:
                textpage = page.get_textpage()
                try:
                    return textpage.get_text_range()
                finally:
                    textpage.close()
            finally:
                page.close()
        finally:
            doc.close()


def pdf_detail_images(file, page_number):
    """Bounded overlapping crops from the original PDF, not an upscaled preview."""
    from .customer_excel import MEDIA_TYPE, detail_images
    if file.media_type == MEDIA_TYPE:
        return detail_images(storage_path(file.storage_key), page_number)
    if file.media_type != "application/pdf":
        return []
    images = []
    with PDF_LOCK:
        doc = pdfium.PdfDocument(storage_path(file.storage_key) / "original")
        try:
            page = doc[page_number - 1]
            bitmap = None
            image = None
            try:
                width, height = page.get_size()
                bitmap = page.render(scale=min(4.0, 3600 / max(width, height)))
                image = bitmap.to_pil().convert("RGB")
                w, h = image.size
                for label, box in (
                    ("左上", (0, 0, (w + 1) // 2 + 60, (h + 1) // 2 + 60)),
                    ("右上", (max(0, w // 2 - 60), 0, w, (h + 1) // 2 + 60)),
                    ("左下", (0, max(0, h // 2 - 60), (w + 1) // 2 + 60, h)),
                    ("右下", (max(0, w // 2 - 60), max(0, h // 2 - 60), w, h)),
                ):
                    with image.crop(tuple(min(v, w if i % 2 == 0 else h)
                                          for i, v in enumerate(box))) as crop:
                        crop.thumbnail((1800, 1800))
                        buffer = io.BytesIO()
                        crop.save(buffer, format="PNG")
                        images.append((label, buffer.getvalue()))
            finally:
                if image is not None:
                    image.close()
                if bitmap is not None:
                    bitmap.close()
                page.close()
        finally:
            doc.close()
    return images


def image_detail_images(file, page_number):
    if file.media_type not in FORMATS.values():
        return []
    with Image.open(storage_path(file.storage_key) / "original") as original:
        if max(original.size) <= 1800:
            return []
        image = ImageOps.exif_transpose(original).convert("RGB")
        w, h = image.size
        results = []
        for index, box in enumerate(((0, 0, w // 2, h // 2), (w // 2, 0, w, h // 2),
                                     (0, h // 2, w // 2, h), (w // 2, h // 2, w, h))):
            with image.crop(box) as crop:
                crop.thumbnail((1800, 1800))
                output = io.BytesIO()
                crop.save(output, "PNG")
                results.append((f"原图局部{index + 1}，与整图重叠，不重复计数", output.getvalue()))
        image.close()
        return results


def image_regions(file):
    """Tile the original pixels after applying camera orientation."""
    with Image.open(storage_path(file.storage_key) / "original") as original:
        with ImageOps.exif_transpose(original) as image:
            width, height = image.size
    columns = max(1, math.ceil(width / settings().image_tile_pixels))
    rows = max(1, math.ceil(height / settings().image_tile_pixels))
    return [(column / columns, row / rows, (column + 1) / columns, (row + 1) / rows)
            for row in range(rows) for column in range(columns)]


def visual_region_content(file, page_number, region):
    if file.media_type == "application/pdf":
        return pdf_region_content(file, page_number, region)
    left, top, right, bottom = region
    with Image.open(storage_path(file.storage_key) / "original") as original:
        with ImageOps.exif_transpose(original) as image:
            width, height = image.size
            px, py = (right - left) * .04, (bottom - top) * .04
            box = (math.floor(max(0, left - px) * width), math.floor(max(0, top - py) * height),
                   math.ceil(min(1, right + px) * width), math.ceil(min(1, bottom + py) * height))
            with image.crop(box) as crop:
                crop.thumbnail((1800, 1800))
                with crop.convert("RGB") as rgb:
                    output = io.BytesIO()
                    rgb.save(output, "PNG")
    return "", output.getvalue()


def save_png(image, target):
    image = ImageOps.exif_transpose(image).convert("RGB")
    image.thumbnail((1800, 1800))
    image.save(target, "PNG", optimize=True)


def prepare_file(data, filename: str):
    source = io.BytesIO(data) if isinstance(data, bytes) else data
    source.seek(0, 2)
    byte_size = source.tell()
    source.seek(0)
    header = source.read(8)
    source.seek(0)
    limit = settings().max_pdf_upload_bytes if header.startswith(b"%PDF-") else settings().max_upload_bytes
    if not byte_size or byte_size > limit:
        raise FileRejected(f"单个文件不能为空或超过 {limit // (1024 * 1024)}MB")
    key = uuid.uuid4().hex
    directory = storage_path(key)
    directory.mkdir(parents=True)
    try:
        digest = hashlib.sha256()
        with (directory / "original").open("wb") as target:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
                target.write(chunk)
        data = b"" if header.startswith(b"%PDF-") else (directory / "original").read_bytes()
        suffix = Path(filename).suffix.casefold()
        if suffix in (".csv", ".tsv", ".txt", ".docx", ".xls") or data.startswith(b"\xd0\xcf\x11\xe0"):
            from .customer_documents import normalize_document
            from .customer_excel import MEDIA_TYPE
            try:
                count = normalize_document(data, filename, directory)
            except Exception as exc:
                raise FileRejected(f"无法读取资料：{str(exc)[:200]}") from exc
            media_type = MEDIA_TYPE
        elif data.startswith(b"PK\x03\x04"):
            from .customer_excel import MEDIA_TYPE, prepare_excel
            try:
                count = prepare_excel(data, directory)
            except Exception as exc:  # untrusted workbook: any parser failure is a rejection
                raise FileRejected(f"无法读取客户Excel：{str(exc)[:200]}") from exc
            media_type = MEDIA_TYPE
        elif header.startswith(b"%PDF-"):
            with PDF_LOCK:
                try:
                    doc = pdfium.PdfDocument(directory / "original")
                except (ValueError, RuntimeError, OSError) as exc:
                    raise FileRejected("PDF 已加密或损坏，无法读取") from exc
                try:
                    count = len(doc)
                    if not 1 <= count <= settings().max_job_pages:
                        raise FileRejected(f"PDF 页数需在 1–{settings().max_job_pages} 页之间")
                finally:
                    doc.close()
            media_type = "application/pdf"
        else:
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("error", Image.DecompressionBombWarning)
                    with Image.open(io.BytesIO(data)) as image:
                        if image.format not in FORMATS or getattr(image, "n_frames", 1) != 1:
                            raise FileRejected("支持单帧JPG、PNG、WebP、PDF、XLSX、XLS、CSV、TSV、TXT和DOCX")
                        media_type = FORMATS[image.format]
                        image.load()
                        save_png(image, directory / "page-1.png")
                count = 1
            except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
                raise FileRejected("文件格式无效或图片像素过大；支持图片、PDF、Excel、CSV、TSV、TXT和DOCX") from exc
        return {"filename": Path(filename.replace("\\", "/")).name[:255] or "customer-file",
                "sha256": digest.hexdigest(), "storage_key": key,
                "media_type": media_type, "byte_size": byte_size, "page_count": count}
    except Exception:
        cleanup_file(key)
        raise


def ensure_pdf_preview(file, page_number):
    target = storage_path(file.storage_key) / f"page-{page_number}.png"
    if file.media_type != "application/pdf" or target.exists():
        return target
    if not 1 <= page_number <= file.page_count:
        raise FileRejected("PDF 页码无效")
    with PDF_LOCK:
        if target.exists():
            return target
        with pdfium.PdfDocument(storage_path(file.storage_key) / "original") as doc:
            with closing(doc[page_number - 1]) as page:
                width, height = page.get_size()
                if not all(math.isfinite(v) and v > 0 for v in (width, height)):
                    raise FileRejected("PDF 页面尺寸无效")
                with closing(page.render(scale=min(2.0, 1800 / max(width, height)))) as bitmap:
                    with bitmap.to_pil() as image:
                        temporary = target.with_suffix(f".{uuid.uuid4().hex}.tmp")
                        try:
                            save_png(image, temporary)
                            temporary.replace(target)
                        finally:
                            temporary.unlink(missing_ok=True)
    return target


def render_pdf_batch(file, start, check=lambda: None):
    end = min(file.page_count + 1, start + settings().pdf_render_batch_pages)
    for page in range(start, end):
        check()
        ensure_pdf_preview(file, page)
    return end - start


def pdf_regions(file, page_number):
    """Normalized rectangles, independent of the PDF page's coordinate origin."""
    with PDF_LOCK:
        with pdfium.PdfDocument(storage_path(file.storage_key) / "original") as doc:
            with closing(doc[page_number - 1]) as page:
                width, height = page.get_size()
    rows = max(settings().pdf_region_rows, min(8, math.ceil(height * 2 / 1800)))
    columns = max(settings().pdf_region_columns, min(4, math.ceil(width * 2 / 1800)))
    return [(column / columns, row / rows, (column + 1) / columns, (row + 1) / rows)
            for row in range(rows) for column in range(columns)]


def pdf_region_content(file, page_number, region):
    """Render only this area, retaining overlap without allocating the full page."""
    left, top, right, bottom = region
    with PDF_LOCK:
        with pdfium.PdfDocument(storage_path(file.storage_key) / "original") as doc:
            with closing(doc[page_number - 1]) as page:
                width, height = page.get_size()
                pad_x, pad_y = (right - left) * .04, (bottom - top) * .04
                x0, x1 = max(0, left - pad_x) * width, min(1, right + pad_x) * width
                y0, y1 = max(0, top - pad_y) * height, min(1, bottom + pad_y) * height
                scale = min(4.0, 1800 / max(x1 - x0, y1 - y0))
                with closing(page.render(scale=scale, crop=(x0, height - y1, width - x1, y0),
                                         limit_image_cache=True)) as bitmap:
                    with bitmap.to_pil() as image:
                        buffer = io.BytesIO()
                        with image.convert("RGB") as rgb:
                            rgb.save(buffer, "PNG")
                # Text coordinates precede page rotation; transform all four corners.
                rotation = page.get_rotation()
                def unrotate(x, y):
                    if rotation == 90:
                        return y, x
                    if rotation == 180:
                        return width - x, y
                    if rotation == 270:
                        return height - y, width - x
                    return x, height - y
                points = [unrotate(x, y) for x in (x0, x1) for y in (y0, y1)]
                bx0, by0, _, _ = page.get_bbox()
                with closing(page.get_textpage()) as textpage:
                    text = textpage.get_text_bounded(
                        left=bx0 + min(p[0] for p in points), right=bx0 + max(p[0] for p in points),
                        bottom=by0 + min(p[1] for p in points), top=by0 + max(p[1] for p in points))
    return text, buffer.getvalue()


def cleanup_file(key):
    path = storage_path(key)
    if path.exists():
        for child in path.iterdir():
            if child.is_file():
                child.unlink()
        path.rmdir()
