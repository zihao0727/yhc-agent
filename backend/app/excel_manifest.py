"""Versioned parser caches retain original previews and never remap existing evidence pages."""
import json
import tempfile
import threading
import uuid
from pathlib import Path

LOCK = threading.Lock()
VERSION = 2


def source_identity(manifest):
    return [(p["sheet"], p["rows"]) for p in manifest["pages"]]


def load_manifest(directory, upgrade=False):
    latest, original = directory / "excel-v2.json", directory / "excel.json"
    if latest.exists():
        return json.loads(latest.read_text(encoding="utf-8"))
    manifest = json.loads(original.read_text(encoding="utf-8"))
    if not upgrade or manifest.get("parser_version", 1) >= VERSION or not (directory / "original").exists():
        return manifest
    from .customer_documents import prepare_normalized
    with LOCK:
        if latest.exists():
            return json.loads(latest.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory(prefix="firefly-parser-") as temporary:
            stage = Path(temporary)
            source = directory / ("normalized.xlsx" if (directory / "normalized.xlsx").is_file() else "original")
            conversion = (json.loads((directory / "conversion.json").read_text(encoding="utf-8"))
                          if (directory / "conversion.json").is_file() else None)
            prepare_normalized(source.read_bytes(), stage, conversion)
            current = json.loads((stage / "excel.json").read_text(encoding="utf-8"))
            if source_identity(current) != source_identity(manifest):
                manifest["upgrade_status"] = "requires_reextraction"
                return manifest
            mapping = {}
            for page in current["pages"]:
                for image in page["images"]:
                    old = image["storage"]
                    if old not in mapping:
                        new = f"excel-image-{2_000_000 + len(mapping)}.png"
                        mapping[old] = new
                        (directory / new).write_bytes((stage / old).read_bytes())
                    image["storage"] = mapping[old]
                for entry in page.get("resource_audit", []):
                    if entry.get("storage") in mapping:
                        entry["storage"] = mapping[entry["storage"]]
            current["upgrade_status"] = "compatible_cache"
            pending = directory / "excel-v2.json.tmp"
            pending.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")
            pending.replace(latest)
            return current


def ensure_preview(directory, page):
    from .customer_excel import render
    manifest = load_manifest(directory, upgrade=True)
    if not 1 <= page <= len(manifest["pages"]):
        raise ValueError("Excel预览页不存在")
    prefix = "page-v2" if (directory / "excel-v2.json").exists() else "page"
    target = directory / f"{prefix}-{page}.png"
    with LOCK:
        if not target.exists():
            temporary = target.with_suffix(f".{uuid.uuid4().hex}.tmp")
            try:
                render(manifest["pages"][page - 1], directory, temporary)
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
    return target
