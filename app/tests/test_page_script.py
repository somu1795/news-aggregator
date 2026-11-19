"""
The page's script (static/retro.js), run in jsdom on the page the server renders: what a reader
sees and can do. The tests themselves are tests/page/page.test.js; they need Node and, once,
`npm ci` in tests/page.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

PAGE_TESTS = Path(__file__).parent / "page"
NODE = shutil.which("node")


def test_the_pages_script(client, tmp_path):
    if not NODE or not (PAGE_TESTS / "node_modules").is_dir():
        if os.environ.get("CI"):  # Never skipped unnoticed there
            pytest.fail("Node and tests/page/node_modules are needed for the page's script tests")
        pytest.skip("needs Node, and `npm ci` in tests/page")
    page = tmp_path / "page.html"
    page.write_text(client.get("/", headers={"Accept": "text/html"}).text)
    moon = tmp_path / "moon.html"  # The moon page, for tests/page/moon.test.js
    moon.write_text(client.get("/moon", headers={"Accept": "text/html"}).text)
    result = subprocess.run([NODE, "--test", "--test-timeout=10000"], cwd=PAGE_TESTS, env={**os.environ, "PAGE_HTML": str(page), "MOON_HTML": str(moon)},
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout[-6000:] + result.stderr[-2000:]
