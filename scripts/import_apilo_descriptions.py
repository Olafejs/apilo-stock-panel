#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from db import init_db, replace_apilo_description_references  # noqa: E402
from description_checks import (  # noqa: E402
    load_apilo_description_export,
    safe_source_name,
)


def main():
    parser = argparse.ArgumentParser(
        description="Importuje wzorcowe opisy produktów z eksportu XLSX Apilo."
    )
    parser.add_argument("xlsx")
    parser.add_argument("--db", default=str(ROOT_DIR / "data" / "db" / "apilo.sqlite3"))
    args = parser.parse_args()

    init_db(args.db)
    records = load_apilo_description_export(args.xlsx)
    count = replace_apilo_description_references(
        args.db,
        records,
        source_name=safe_source_name(args.xlsx),
    )
    print(json.dumps({"imported": count, "source": safe_source_name(args.xlsx)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
