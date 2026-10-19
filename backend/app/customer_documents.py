"""Normalize text, Word and legacy tables into traceable, read-only source cells."""
import csv
import hashlib
import io
import json
import struct
import zipfile
from pathlib import Path

from openpyxl import Workbook
from openpyxl.drawing.image import Image as WorkbookImage
from openpyxl.utils import get_column_letter

from .customer_excel import MAX_CELLS, MAX_COLS, MAX_EXPANDED, MAX_ROWS, prepare_excel, relationships, xml

WORD = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
      "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
TEXT_TYPES = {".csv": "text/csv", ".tsv": "text/tab-separated-values", ".txt": "text/plain"}


def legacy_has_objects(book):
    # xlrd supplies the decoded BIFF stream; inspect record types, never execute content.
    offset, end = book.base, book.base + book.stream_len
    while offset + 4 <= end:
        kind, size = struct.unpack_from("<HH", book.mem, offset)
        if kind in (0x00EC, 0x005D):
            return True
        if kind == size == 0:
            break
        offset += 4 + size
    return False


def decode_text(data):
    encodings = ("utf-16",) if data.startswith((b"\xff\xfe", b"\xfe\xff")) else ("utf-8-sig", "gb18030")
    for encoding in encodings:
        try:
            text = data.decode(encoding)
        except UnicodeError:
            continue
        if "\x00" not in text and not any(ord(char) < 32 and char not in "\r\n\t" for char in text):
            return text, encoding
    raise ValueError("文字编码无法识别，请转换为UTF-8或GB18030")


def normalize_document(data, filename, directory):
    suffix = Path(filename).suffix.casefold()
    book = Workbook()
    book.remove(book.active)
    locations, warnings, unresolved, streams, written_rows = {}, [], [], [], {}
    total_cells = 0

    def add(sheet, values, origin):
        nonlocal total_cells
        row = written_rows.get(sheet.title, 0) + 1
        if row > MAX_ROWS:
            raise ValueError("转换后的行数超过2000，请拆分上传")
        if len(values) > MAX_COLS:
            raise ValueError("转换后的列数超过60，请拆分上传")
        written_rows[sheet.title] = row
        for column, value in enumerate(values, 1):
            if value is None:
                continue
            text = str(value)
            if len(text) > 4000:
                raise ValueError("单项文本超过4000字，请拆分上传")
            cell = sheet.cell(row, column, text)
            cell.data_type = "s"
            locations[f"{sheet.title}!{cell.coordinate}"] = {**origin, "column": column}
            total_cells += 1
            if total_cells > MAX_CELLS:
                raise ValueError("转换后的单元格超过50000")
        return row

    try:
        if suffix in TEXT_TYPES:
            text, encoding = decode_text(data)
            sheet = book.create_sheet("正文" if suffix == ".txt" else "清单")
            if suffix == ".txt":
                rows = ([line] for line in text.splitlines())
            else:
                delimiter = "\t" if suffix == ".tsv" else ","
                try:
                    delimiter = csv.Sniffer().sniff(text[:16384], delimiters=",;\t").delimiter
                except csv.Error:
                    pass
                rows = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
            for index, values in enumerate(rows, 1):
                if any(str(value).strip() for value in values):
                    add(sheet, values, {"format": suffix[1:], "record": index})
            media_type = TEXT_TYPES[suffix]
            warnings.append(f"文字按{encoding}读取，源记录号保留在origin；以=开头的内容仍按原文保留，不执行公式。")
        elif suffix == ".xls" or data.startswith(b"\xd0\xcf\x11\xe0"):
            import xlrd
            legacy = xlrd.open_workbook(file_contents=data, on_demand=True, ragged_rows=True)
            try:
                for source in legacy.sheets():
                    if source.visibility:
                        continue
                    if source.nrows > MAX_ROWS or source.ncols > MAX_COLS:
                        raise ValueError("旧版XLS行列范围过大，请拆分上传")
                    sheet = book.create_sheet(source.name)
                    for index in range(source.nrows):
                        values = []
                        for cell in source.row(index):
                            value = cell.value
                            if cell.ctype == xlrd.XL_CELL_DATE:
                                value = xlrd.xldate_as_datetime(value, legacy.datemode).isoformat()
                            elif cell.ctype == xlrd.XL_CELL_NUMBER and float(value).is_integer():
                                value = int(value)
                            values.append(value)
                        add(sheet, values, {"format": "xls", "sheet": source.name, "row": index + 1})
                if legacy_has_objects(legacy):
                    unresolved.extend({"sheet": sheet.title, "row": 0, "kind": "xls_object",
                        "status": "unresolved", "reason": "旧XLS含图形或对象，须补充PDF或图片核对"}
                        for sheet in book)
                warnings.append("旧XLS仅转换可见单元格和公式缓存值，不执行公式或宏；图形及嵌入对象须另附PDF或图片核对。")
            finally:
                legacy.release_resources()
            media_type = "application/vnd.ms-excel"
        elif data.startswith(b"PK\x03\x04"):
            media_type = WORD
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                entries = archive.infolist()
                names = archive.namelist()
                if (len(entries) > 3000 or len(names) != len(set(names))
                        or sum(entry.file_size for entry in entries) > MAX_EXPANDED
                        or any(entry.flag_bits & 1 or entry.file_size > 32 * 1024 * 1024 for entry in entries)):
                    raise ValueError("Word压缩包已加密或展开资源过大")
                if "word/document.xml" not in names:
                    raise ValueError("不是有效的DOCX文档")
                if any("vbaproject" in name.casefold() or "/embeddings/" in name.casefold() for name in names):
                    raise ValueError("Word包含宏或嵌入对象，请另存为普通DOCX")
                for name in names:
                    if name.casefold().endswith((".xml", ".rels")):
                        xml(archive, name)
                if any("macroenabled" in node.get("ContentType", "").casefold()
                       for node in xml(archive, "[Content_Types].xml")):
                    raise ValueError("不接受带宏Word，请另存为普通DOCX")
                parts = ["word/document.xml", *sorted(name for name in names if name.startswith(
                    ("word/header", "word/footer")) and name.endswith(".xml")),
                    *[name for name in ("word/footnotes.xml", "word/endnotes.xml") if name in names]]
                for part in parts:
                    sheet = book.create_sheet("正文" if part == "word/document.xml" else Path(part).stem[:31])
                    rels = relationships(archive, part, allow_missing=True)

                    def text_of(node):
                        return "".join(element.text or "" if element.tag == f'{{{NS["w"]}}}t'
                                       else "\n" if element.tag == f'{{{NS["w"]}}}br' else "\t"
                                       for element in node.iter() if element.tag in {
                                           f'{{{NS["w"]}}}t', f'{{{NS["w"]}}}br', f'{{{NS["w"]}}}tab'})

                    def images_of(node, row, column):
                        for blip in node.findall(".//a:blip", NS):
                            asset = rels.get(blip.get(f'{{{NS["r"]}}}embed'))
                            try:
                                if not asset:
                                    raise ValueError("图片为外部链接或缺失")
                                stream = io.BytesIO(archive.read(asset))
                                image = WorkbookImage(stream)
                                streams.append(stream)
                                sheet.add_image(image, f"{get_column_letter(column)}{row}")
                            except (OSError, ValueError) as exc:
                                cell = sheet.cell(row, column)
                                cell.value = f"{cell.value or ''} [内嵌图未读取]"
                                cell.data_type = "s"
                                unresolved.append({"sheet": sheet.title, "row": row, "cell": f"{get_column_letter(column)}{row}",
                                                   "kind": "word_image", "status": "unresolved",
                                                   "reason": str(exc)[:200]})

                    def blocks(parent, path):
                        for index, node in enumerate(parent, 1):
                            location = {"format": "docx", "part": part, "block": f"{path}/{index}"}
                            if node.tag == f'{{{NS["w"]}}}p':
                                text = text_of(node)
                                if text.strip() or node.findall(".//a:blip", NS):
                                    chunks = [text[offset:offset + 4000] for offset in range(0, len(text), 4000)] or [""]
                                    for offset, chunk in enumerate(chunks):
                                        row = add(sheet, [chunk], {**location, "text_offset": offset * 4000})
                                        if offset == 0:
                                            images_of(node, row, 1)
                            elif node.tag == f'{{{NS["w"]}}}tbl':
                                for tr_index, tr in enumerate(node.findall("w:tr", NS), 1):
                                    cells = tr.findall("w:tc", NS)
                                    row = add(sheet, [text_of(cell) for cell in cells],
                                              {**location, "table_row": tr_index})
                                    for column, cell in enumerate(cells, 1):
                                        images_of(cell, row, column)
                            else:
                                blocks(node, location["block"])

                    blocks(xml(archive, part), "")
                warnings.append("Word转为段落及表格来源视图；页号为资料分段号，不是Word打印页码。origin保留原XML位置，内嵌图按段落或表格单元格关联。")
        else:
            raise ValueError("不支持的资料格式")
        if not locations and not any(sheet._images for sheet in book):
            raise ValueError("文件没有可读取的文字、表格或图片")
        output = io.BytesIO()
        book.save(output)
        normalized = output.getvalue()
        (directory / "normalized.xlsx").write_bytes(normalized)
        conversion = {"format": suffix.lstrip("."), "original_media_type": media_type,
                      "normalized_sha256": hashlib.sha256(normalized).hexdigest(),
                      "locations": locations, "warnings": warnings, "unresolved": unresolved}
        (directory / "conversion.json").write_text(json.dumps(conversion, ensure_ascii=False), encoding="utf-8")
        return prepare_normalized(normalized, directory, conversion)
    finally:
        book.close()
        for stream in streams:
            stream.close()


def prepare_normalized(data, directory, conversion=None):
    count = prepare_excel(data, directory)
    if conversion is None and (directory / "conversion.json").is_file():
        conversion = json.loads((directory / "conversion.json").read_text(encoding="utf-8"))
    if conversion:
        path = directory / "excel.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["conversion"] = {key: value for key, value in conversion.items() if key != "locations"}
        manifest["resource_summary"]["discovered"] += len(conversion["unresolved"])
        manifest["resource_summary"]["unresolved"] += len(conversion["unresolved"])
        for page in manifest["pages"]:
            page["warnings"].extend(conversion["warnings"])
            page["resource_audit"].extend(item for item in conversion["unresolved"]
                if item["sheet"] == page["sheet"] and (
                    not item.get("row") or page["row_start"] <= item["row"] <= page["row_end"]))
            for row in [*page["rows"], *page["header_rows"]]:
                for cell in row["cells"]:
                    cell["origin"] = conversion["locations"].get(f'{page["sheet"]}!{cell["cell"]}')
        manifest["notice"] += "\n" + "\n".join(conversion["warnings"])
        path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return count
