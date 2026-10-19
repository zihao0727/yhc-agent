"""Invoke audited local maintenance through the same authenticated API as the UI."""
import argparse
import json
from urllib.request import Request, urlopen

from app.config import settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_ids", nargs="+", type=int)
    parser.add_argument("--action", choices=(
        "revalidate", "refresh_candidates", "retry_failed", "disable_auto_resume"), default="revalidate")
    parser.add_argument("--line", action="append", default=[])
    parser.add_argument("--reason", required=True)
    parser.add_argument("--port", type=int, default=8002)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Invalid local API port.")
    headers = {"X-Admin-Token": settings().admin_token, "Content-Type": "application/json"}
    base = f"http://127.0.0.1:{args.port}/api"
    for identity in args.job_ids:
        with urlopen(Request(f"{base}/jobs/{identity}", headers=headers), timeout=60) as response:
            job = json.load(response)
        data = {"revision": job["revision"], "action": args.action,
                "line_ids": args.line, "reason": args.reason}
        with urlopen(Request(f"{base}/jobs/{identity}/maintenance", headers=headers,
                             data=json.dumps(data).encode("utf-8"), method="POST"),
                     timeout=60) as response:
            result = json.load(response)
        print(json.dumps({
            "job_id": identity, "revision_before": job["revision"],
            "revision_after": result["job"]["revision"],
            "summary": result["job"]["quote_workspace"]["summary"],
            "maintenance": result["maintenance"],
        }, ensure_ascii=False))


if __name__ == "__main__":
    main()
