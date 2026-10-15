"""Reset demo data and local state for OFFERHUNT (Milestone M8).

Safely clears temporary test artifacts, re-initializes offerhunt.db schema,
and restores seed demo spreadsheets and directories without removing gitkeeps.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from agent.contracts import InternshipRecord
from mcp_tools.spreadsheet import upsert_records_to_spreadsheet
from storage.database import DEFAULT_DB_PATH, init_database


def reset_demo_environment() -> None:
    print("=== OFFERHUNT Demo Environment Reset ===")

    artifacts_dir = PROJECT_ROOT / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    # 1. Clear old temporary files in artifacts while preserving .gitkeep
    print(f"Cleaning artifacts directory: {artifacts_dir}")
    for item in artifacts_dir.iterdir():
        if item.name == ".gitkeep":
            continue
        try:
            if item.is_file() or item.is_symlink():
                item.unlink()
            elif item.is_dir():
                shutil.rmtree(item)
        except Exception as e:
            print(f"  Warning: could not delete {item.name}: {e}")

    # Re-ensure drafts directory exists
    drafts_dir = artifacts_dir / "drafts"
    drafts_dir.mkdir(parents=True, exist_ok=True)

    # 2. Re-initialize SQLite database (offerhunt.db)
    print(f"Initializing clean database schema at: {DEFAULT_DB_PATH}")
    init_database(DEFAULT_DB_PATH)

    # 3. Populate default demo spreadsheet from seed fixtures
    fixtures_file = PROJECT_ROOT / "data" / "internships_demo.json"
    if fixtures_file.exists():
        print(f"Seeding demo spreadsheet from: {fixtures_file}")
        with open(fixtures_file, "r", encoding="utf-8") as f:
            demo_items = json.load(f)

        records = [InternshipRecord(**item) for item in demo_items]
        res = upsert_records_to_spreadsheet(
            records=records,
            target_path="offers.xlsx",
            allowed_dir=artifacts_dir,
        )
        print(f"  Seeded {res.inserted} demo internship records into offers.xlsx (SHA256: {res.sha256[:12]}...)")
    else:
        print("  Notice: data/internships_demo.json not found; skipping spreadsheet seed.")

    print("\n[SUCCESS] OFFERHUNT demo environment successfully reset.")


if __name__ == "__main__":
    reset_demo_environment()
