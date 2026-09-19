"""Explicit, audited one-time activation of existing draft prices."""
import argparse

from .database import SessionLocal
from .services import activate_draft_prices


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", required=True)
    parser.add_argument("--reason", required=True)
    args = parser.parse_args()
    if len(args.reason.strip()) < 2:
        parser.error("A meaningful audit reason is required.")
    with SessionLocal.begin() as db:
        count = activate_draft_prices(db, args.reason.strip())
    print(f"Activated {count} draft prices; pricing facts preserved; revisions and audit records saved.")


if __name__ == "__main__":
    main()
