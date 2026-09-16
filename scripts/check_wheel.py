"""Exercise the installed package, not imports from the checkout."""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
origin = Path(importlib.util.find_spec("cataloglint").origin).resolve()
assert ROOT not in origin.parents, f"Expected an installed package, got {origin}"

CASES = [
    [["import.xml", "offers.xml", "--strict"], 0],
    [["import.xml", "broken-offers.xml"], 1],
    [["import.xml", "missing.xml"], 2],
]

for filenames, expected_code in CASES:
    arguments = [
        str(ROOT / "examples" / value) if not value.startswith("--") else value
        for value in filenames
    ]
    for as_json in (True, False):
        result = subprocess.run(
            [sys.executable, "-m", "cataloglint", *arguments, *(["--json"] if as_json else [])],
            capture_output=True,
            env={**os.environ, "PYTHONIOENCODING": "ascii:strict"},
        )
        assert result.returncode == expected_code, (arguments, result.stdout, result.stderr)
        assert not result.stderr, result.stderr
        if as_json:
            payload = json.loads(result.stdout)
            assert payload["exit_code"] == expected_code
            assert payload["ok"] is (expected_code == 0)

print(
    "Installed wheel: valid, invalid and missing inputs return the expected reports and exit codes."
)
