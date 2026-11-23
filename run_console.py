#!/usr/bin/env python3
"""Lead Finder -- simple console front-end for non-technical users.

Reads search settings from settings.txt next to this program, then runs
the full Lead Finder pipeline and reports where the Excel output landed.
This is the entry point packaged into the standalone client executable.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def app_dir() -> Path:
    """Directory the exe (or this script) actually lives in.

    Deliberately not BASE_DIR from config.py: under a frozen PyInstaller
    build that points inside the temporary extraction folder, which is
    wiped when the program exits -- settings.txt and the output folder
    both need to live next to the real .exe instead.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = app_dir()
sys.path.insert(0, str(Path(__file__).resolve().parent))

SETTINGS_NAME = "settings.txt"
SETTINGS_PATH = APP_DIR / SETTINGS_NAME

BANNER = r"""
  _                    _   _____ _           _
 | |    ___  __ _  __| | |  ___(_)_ __   __| | ___ _ __
 | |   / _ \/ _` |/ _` | | |_  | | '_ \ / _` |/ _ \ '__|
 | |__|  __/ (_| | (_| | |  _| | | | | | (_| |  __/ |
 |_____\___|\__,_|\__,_| |_|   |_|_| |_|\__,_|\___|_|

                Client console  --  type QUIT any time to exit
"""

DEFAULT_SETTINGS = """\
# Lead Finder settings
# ---------------------
# Edit the values after each "=" sign, save this file, then run LeadFinder.exe
# again to pick up the changes. Lines starting with # are notes and ignored.

# Your Google Maps / Places API key (required). Get one from
# https://console.cloud.google.com -- enable the Places API and the
# Geocoding API for it.
GOOGLE_MAPS_API_KEY =

# Where to look.
COUNTRY = United States
STATE =
CITY = Austin
CATEGORY = Roofing

# Optional filters -- leave blank to use the defaults.
MAX_RESULTS = 60
MIN_REVIEWS = 0
MIN_RATING = 0

# Set to YES to skip the slower website performance (PageSpeed) check.
SKIP_PAGESPEED = NO
"""


def parse_settings(path: Path) -> dict:
    values: dict = {}
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip().upper()] = value.strip()
    return values


def build_argv(values: dict) -> list:
    argv: list = []

    def add(flag: str, key: str) -> None:
        value = values.get(key, "")
        if value:
            argv.extend([flag, value])

    add("--country", "COUNTRY")
    add("--state", "STATE")
    add("--city", "CITY")
    add("--category", "CATEGORY")
    add("--max-results", "MAX_RESULTS")
    add("--min-reviews", "MIN_REVIEWS")
    add("--min-rating", "MIN_RATING")
    if values.get("SKIP_PAGESPEED", "").strip().lower() in ("yes", "y", "true", "1"):
        argv.append("--no-pagespeed")
    return argv


def print_settings(values: dict) -> None:
    key = values.get("GOOGLE_MAPS_API_KEY", "").strip()
    location = ", ".join(
        part for part in (values.get("CITY", ""), values.get("STATE", ""), values.get("COUNTRY", "")) if part
    )
    print("Current settings (from settings.txt):")
    print(f"  Location : {location or '(not set)'}")
    print(f"  Category : {values.get('CATEGORY', '') or '(not set)'}")
    print(f"  API key  : {'set (' + key[:6] + '...)' if key else 'NOT SET -- edit settings.txt first'}")
    print()


def ensure_settings_file() -> bool:
    if SETTINGS_PATH.exists():
        return True
    SETTINGS_PATH.write_text(DEFAULT_SETTINGS, encoding="utf-8")
    print(f"Created {SETTINGS_NAME} next to this program.")
    print("Open it, add your API key and search details, save it, then run this program again.\n")
    return False


def run_once(values: dict) -> None:
    api_key = values.get("GOOGLE_MAPS_API_KEY", "").strip()
    if not api_key:
        print("No API key set in settings.txt yet -- add one, save the file, and try again.\n")
        return

    os.environ["GOOGLE_MAPS_API_KEY"] = api_key
    os.environ["LEAD_FINDER_OUTPUT_DIR"] = str(APP_DIR / "output")
    os.environ["LEAD_FINDER_CACHE_DIR"] = str(APP_DIR / ".cache")

    from main import main as lead_finder_main

    argv = build_argv(values)
    print("Starting -- this can take a few minutes, depending on how many businesses")
    print("are found and whether the performance check is on.\n")
    code = lead_finder_main(argv)
    print()
    if code == 0:
        print(f"Done. Your Excel file is in: {APP_DIR / 'output'}\n")
    else:
        print("The run did not finish cleanly -- see the messages above.\n")


def main() -> None:
    print(BANNER)
    if not ensure_settings_file():
        input("Press Enter to exit...")
        return

    while True:
        values = parse_settings(SETTINGS_PATH)
        print_settings(values)
        command = input("Type RUN to start, EDIT to open settings.txt, or QUIT to exit: ").strip().lower()
        print()
        if command in ("quit", "q", "exit"):
            break
        if command in ("edit", "e"):
            try:
                os.startfile(str(SETTINGS_PATH))  # type: ignore[attr-defined]
            except Exception:
                print(f"Open {SETTINGS_PATH} in a text editor, make your changes, and save it.\n")
            input("Press Enter once you've saved your changes...")
            print()
            continue
        run_once(values)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
    input("Press Enter to close this window...")
