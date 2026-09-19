import hashlib
import io
import threading
import uuid
import warnings
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
    if getattr(file, "media_type", "") != "application/pdf":
        return ""
    with PDF_LOCK:
        doc = pdfium.PdfDocument(storage_path(file.storage_key) / "original")
        try:
            page = doc[page_number - 1]
            try:
                textpage = page.get_textpage()
                try:
                    return textpage.get_text_range()[:40000]
                finally:
                    textpage.close()
            finally:
                page.close()
        finally:
            doc.close()


def pdf_detail_images(file, page_number):
    """Bounded overlapping crops from the original PDF, not an upscaled preview."""
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


def save_png(image, target):
    image = ImageOps.exif_transpose(image).convert("RGB")
    image.thumbnail((1800, 1800))
    image.save(target, "PNG", optimize=True)


def prepare_file(data: bytes, filename: str):
    if not data or len(data) > settings().max_upload_bytes:
        raise FileRejected("单个文件不能为空或超过 20MB")
    key = uuid.uuid4().hex
    directory = storage_path(key)
    directory.mkdir(parents=True)
    try:
        if data.startswith(b"%PDF-"):
            with PDF_LOCK:
                try:
                    doc = pdfium.PdfDocument(data)
                except Exception as exc:
                    raise FileRejected("PDF 已加密或损坏，无法读取") from exc
                try:
                    count = len(doc)
                    if not 1 <= count <= settings().max_job_pages:
                        raise FileRejected("PDF 页数需在 1–20 页之间")
                    for index in range(count):
                        page = doc[index]
                        bitmap = None
                        try:
                            width, height = page.get_size()
                            if min(width, height) <= 0:
                                raise FileRejected("PDF 页面尺寸无效")
                            scale = min(2.0, 1800 / max(width, height))
                            bitmap = page.render(scale=scale)
                            save_png(bitmap.to_pil(), directory / f"page-{index + 1}.png")
                        finally:
                            if bitmap:
                                bitmap.close()
                            page.close()
                finally:
                    doc.close()
            media_type = "application/pdf"
        else:
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("error", Image.DecompressionBombWarning)
                    with Image.open(io.BytesIO(data)) as image:
                        if image.format not in FORMATS or getattr(image, "n_frames", 1) != 1:
                            raise FileRejected("仅支持单帧 JPG、PNG、WebP 图片或 PDF，不支持 Excel")
                        media_type = FORMATS[image.format]
                        image.load()
                        save_png(image, directory / "page-1.png")
                count = 1
            except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
                raise FileRejected("文件不是有效图片/PDF，或图片像素过大") from exc
        (directory / "original").write_bytes(data)
        return {"filename": Path(filename.replace("\\", "/")).name[:255] or "customer-file",
                "sha256": hashlib.sha256(data).hexdigest(), "storage_key": key,
                "media_type": media_type, "byte_size": len(data), "page_count": count}
    except Exception:
        cleanup_file(key)
        raise


def cleanup_file(key):
    path = storage_path(key)
    if path.exists():
        for child in path.iterdir():
            if child.is_file():
                child.unlink()
        path.rmdir()
