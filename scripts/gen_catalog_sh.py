#!/usr/bin/env python3
"""
gen_catalog_sh.py — Generates apps/catalog.sh from apps/catalog.json
Run after editing catalog.json to keep the Bash TUI in sync.
Usage: python3 scripts/gen_catalog_sh.py
"""
import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
CATALOG_JSON = os.path.join(REPO_ROOT, "apps", "catalog.json")
CATALOG_SH = os.path.join(REPO_ROOT, "apps", "catalog.sh")

def main():
    # Read the JSON catalog
    print(f"Reading {CATALOG_JSON}...")
    with open(CATALOG_JSON) as f:
        data = json.load(f)

    apps = data["apps"]
    print(f"Found {len(apps)} apps")

    # Build the Bash script
    lines = [
        "#!/bin/bash",
        "# =============================================================================",
        "# apps/catalog.sh — AUTO-GENERATED. DO NOT EDIT MANUALLY.",
        "# Run: python3 scripts/gen_catalog_sh.py",
        f"# Total apps: {len(apps)}",
        "# =============================================================================",
        "",
        "# All app IDs (space-separated)",
        "CATALOG_IDS=(" + " ".join(f'"{a["id"]}"' for a in apps) + ")",
        "",
        "# Associative arrays — one entry per app",
        "declare -A APP_NAME APP_CATEGORY APP_IMAGE APP_DESCRIPTION APP_PORTS APP_ICON APP_NOTES",
        "",
    ]

    # Add each app's data
    for a in apps:
        aid = a["id"]
        ports = " ".join(a.get("ports", []))
        lines += [
            f'APP_NAME["{aid}"]={json.dumps(a["name"])}',
            f'APP_CATEGORY["{aid}"]={json.dumps(a["category"])}',
            f'APP_IMAGE["{aid}"]={json.dumps(a["image"])}',
            f'APP_DESCRIPTION["{aid}"]={json.dumps(a.get("description",""))}',
            f'APP_PORTS["{aid}"]={json.dumps(ports)}',
            f'APP_ICON["{aid}"]={json.dumps(a.get("icon",""))}',
            f'APP_NOTES["{aid}"]={json.dumps(a.get("notes",""))}',
            "",
        ]

    # Add helper functions
    lines += [
        "# Helper: get all apps in a category",
        "catalog_apps_in_category() {",
        '    local cat="$1"',
        "    local result=()",
        '    for id in "${CATALOG_IDS[@]}"; do',
        '        if [[ "${APP_CATEGORY[$id]}" == "$cat" ]]; then',
        '            result+=("$id")',
        "        fi",
        "    done",
        '    echo "${result[@]}"',
        "}",
        "",
        "# Helper: list unique categories",
        "catalog_categories() {",
        "    local -A seen",
        '    for id in "${CATALOG_IDS[@]}"; do',
        '        local cat="${APP_CATEGORY[$id]}"',
        '        if [[ -z "${seen[$cat]+x}" ]]; then',
        '            echo "$cat"',
        '            seen[$cat]=1',
        "        fi",
        "    done",
        "}",
    ]

    # Write the file
    print(f"Writing {CATALOG_SH}...")
    with open(CATALOG_SH, "w") as f:
        f.write("\n".join(lines) + "\n")
    
    # Make it executable
    os.chmod(CATALOG_SH, 0o755)

    print(f"✓ Generated {CATALOG_SH} ({len(apps)} apps)")
    return 0

if __name__ == "__main__":
    sys.exit(main())
