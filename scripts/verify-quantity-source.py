"""Offline quantity recovery check against any stored Excel manifest; no model or SQL writes."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from openpyxl.utils.cell import coordinate_to_tuple

from app.customer_excel import table_columns, validate_excel_rows
from app.quantity_validation import validate_quantity_evidence
from app.requirements_schemas import ExtractedLine, ExtractionResult


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    result = ExtractionResult(lines=[])
    sources = {}
    for page in manifest["pages"]:
        key = (1, page["page"])
        sources[key] = page
        columns, header = table_columns(page)
        if "product" not in columns:
            continue
        for row in page["rows"]:
            cells = {coordinate_to_tuple(c["cell"])[1]: c for c in row["cells"]}
            name, quantity = cells.get(columns["product"]), cells.get(columns.get("quantity"))
            if row["row"] <= header or not name or not quantity:
                continue
            # Simulate the legacy failure: a model treats every scalar as one piece.
            line = ExtractedLine.model_validate({
                "product": name["value"][:150], "quantity": 1,
                "evidence": [{"file_id": 1, "page": page["page"], "sheet": page["sheet"],
                              "cell": name["cell"], "text": name["value"][:1500]}],
                "quantity_evidence": [{"file_id": 1, "page": page["page"], "sheet": page["sheet"],
                                       "cell": quantity["cell"], "text": quantity["value"][:1500],
                                       "quantity": 1}]})
            validate_quantity_evidence(line, {key: json.dumps(page)}, recover_semantic=True)
            result.lines.append(line)
    result = validate_excel_rows(result, sources)
    before = result.model_dump(mode="json")
    for line in result.lines:
        validate_quantity_evidence(line, {key: json.dumps(value) for key, value in sources.items()},
                                   recover_semantic=True)
    result = validate_excel_rows(result, sources)
    assert before == result.model_dump(mode="json"), "Repeated validation changed the result"
    print(json.dumps({
        "offline_only": True, "model_calls": 0, "source_modified": False,
        "pages": len(sources), "products": len(result.lines),
        "verified_piece_counts": sum(line.quantity is not None for line in result.lines),
        "unknown_piece_counts": sum(line.quantity is None for line in result.lines),
        "engineering_quantities": [{"value": str(m.value), "unit": m.unit,
                                    "cells": [e.cell for e in m.evidence]}
                                   for line in result.lines for m in line.source_measurements],
        "rejected_count_claims": sum(e.validation_status == "rejected"
                                     for line in result.lines for e in line.quantity_evidence),
        "idempotent": True,
    }, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
