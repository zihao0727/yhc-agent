"""Container entrypoint. This file must never execute generated code on the host."""
import json
from pathlib import Path
import resource
import runpy
import sys
import traceback

resource.setrlimit(resource.RLIMIT_FSIZE, (2 * 1024 * 1024, 2 * 1024 * 1024))
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
resource.setrlimit(resource.RLIMIT_CPU, (40, 42))
try:
    # Logs and scratch files live only on the size-limited container tmpfs.
    with open("/work/script.log", "w", encoding="utf-8") as log:
        sys.stdout = sys.stderr = log
        runpy.run_path("/input/script.py", run_name="__main__")
    result = Path("/work/result.json")
    if result.stat().st_size > 1024 * 1024:
        raise ValueError("result.json exceeds 1 MiB")
    payload = {"ok": True, "output": json.loads(result.read_text(encoding="utf-8"))}
except BaseException:
    payload = {"ok": False, "error": traceback.format_exc(limit=6)[-6000:]}
finally:
    sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__
try:
    with open("/work/script.log", "rb") as log:
        payload["log"] = log.read(12000).decode("utf-8", errors="replace")
except OSError:
    pass
print(json.dumps(payload, ensure_ascii=False, allow_nan=False))
