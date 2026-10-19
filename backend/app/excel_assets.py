"""Resolve embedded image references offline; unresolved resources remain explicit."""
import io
import re
import warnings

from PIL import Image, ImageOps
from openpyxl.utils import coordinate_to_tuple


def children(node, name):
    return [child for child in node if child.tag.rsplit("}", 1)[-1] == name]


def embedded_index(archive):
    from .customer_excel import NS, relationships, xml
    index = {}
    if "xl/cellimages.xml" in archive.namelist():
        rels = relationships(archive, "xl/cellimages.xml")
        for picture in xml(archive, "xl/cellimages.xml").findall(".//d:pic", NS):
            props, blip = picture.find(".//d:cNvPr", NS), picture.find(".//a:blip", NS)
            if props is not None and blip is not None:
                key = props.get("name")
                target = rels.get(blip.get(f'{{{NS["r"]}}}embed'))
                # Duplicate identifiers are ambiguous, never pick an arbitrary image.
                index[key] = None if key in index else target
    return index


def rich_image_index(archive):
    from .customer_excel import NS, relationships, xml
    parts = ("xl/metadata.xml", "xl/richData/rdrichvalue.xml",
             "xl/richData/rdrichvaluestructure.xml", "xl/richData/richValueRel.xml")
    if not all(part in archive.namelist() for part in parts):
        return {}
    metadata, values, structures, links = [xml(archive, part) for part in parts]
    rels = relationships(archive, parts[3])
    types = metadata.find("s:metadataTypes", NS)
    future = metadata.find("s:futureMetadata[@name='XLRICHVALUE']", NS)
    records = metadata.find("s:valueMetadata", NS)
    if types is None or future is None or records is None:
        return {}
    result = {}
    for number, record in enumerate(records, 1):
        try:
            refs = children(record, "rc")
            if len(refs) != 1:
                continue
            ref = refs[0]
            type_index, future_index = int(ref.get("t")) - 1, int(ref.get("v"))
            if min(type_index, future_index) < 0 or types[type_index].get("name") != "XLRICHVALUE":
                continue
            rvb = next(n for n in future[future_index].iter() if n.tag.rsplit("}", 1)[-1] == "rvb")
            value_index = int(rvb.get("i"))
            if value_index < 0:
                continue
            value = values[value_index]
            structure_index = int(value.get("s"))
            if structure_index < 0:
                continue
            structure = structures[structure_index]
            keys = [node.get("n") for node in structure]
            if structure.get("t") != "_localImage" or "_rvRel:LocalImageIdentifier" not in keys:
                continue
            link_index = int(value[keys.index("_rvRel:LocalImageIdentifier")].text)
            if link_index < 0:
                continue
            result[number] = rels.get(links[link_index].get(f'{{{NS["r"]}}}id'))
        except (ValueError, TypeError, IndexError, StopIteration):
            continue
    return result


def cell_image_references(archive, cells):
    from .customer_excel import NS
    wps, rich = embedded_index(archive), rich_image_index(archive)
    for cell in cells:
        formula = cell.findtext("s:f", "", NS)
        if re.search(r"(?:^|[.])DISPIMG\s*\(", formula, re.I):
            match = re.fullmatch(r'(?:_xlfn\.)?DISPIMG\s*\(\s*"([^"]+)"\s*[,;]\s*1\s*\)', formula, re.I)
            yield cell.get("r"), wps.get(match[1]) if match else None, "cell_image"
        elif cell.get("vm") is not None:
            try:
                target = rich.get(int(cell.get("vm")))
            except ValueError:
                target = None
            yield cell.get("r"), target, "rich_value"
        elif re.search(r"(?:^|[.])IMAGE\s*\(", formula, re.I):
            yield cell.get("r"), None, "external_image"


def save_image(archive, target, directory, key):
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(archive.read(target))) as image:
            if image.format not in ("JPEG", "PNG", "WEBP") or getattr(image, "n_frames", 1) != 1:
                raise ValueError("unsupported_image")
            image.load()
            original = ImageOps.exif_transpose(image).convert("RGB")
            try:
                original.thumbnail((3600, 3600))
                original.save(directory / key, optimize=True)
            finally:
                original.close()


def resolve_cell_images(archive, cells, directory, start):
    images, audit = [], []
    for address, target, kind in cell_image_references(archive, cells):
        row, _ = coordinate_to_tuple(address)
        entry = {"cell": address, "row": row, "kind": kind, "status": "unresolved"}
        if target:
            key = f"excel-image-{start + len(images) + 1}.png"
            try:
                save_image(archive, target, directory, key)
            except (ValueError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning):
                entry["reason"] = "unsupported_or_corrupt_image"
            else:
                images.append({"row": row, "cell": address, "storage": key})
                entry.update(status="resolved", storage=key)
        else:
            entry["reason"] = "missing_or_external_reference"
        audit.append(entry)
    return images, audit
