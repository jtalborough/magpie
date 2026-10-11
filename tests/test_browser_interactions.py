import json
from pathlib import Path
import re
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def browser_check_payload():
    sources = {}
    for filename, names in {
        "index.html": (
            "clearChat", "leaveItemEdit", "submitEditItem", "loadAssistantStatus", "load",
            "renderContainer", "clearBinInventory", "initializeCabinetWorkspace", "startEditItem",
            "selectBin", "selectChildLocation", "renderGrid", "cellFor", "renderError",
        ),
        "mobile.html": ("resultRows", "escapeHtml"),
    }.items():
        html = (ROOT / "src/searchable_storage_led/static" / filename).read_text()
        for name in names:
            match = re.search(
                rf"(?ms)^    (?:async )?function {re.escape(name)}\(.*?^    \}}", html
            )
            if match is None:
                match = re.search(rf"(?m)^    const {re.escape(name)} = .*;$", html)
            assert match, f"Missing browser function {name}"
            sources[name] = match.group()
    return {
        "sources": sources,
        "harness": (ROOT / "tests/browser_interactions.js").read_text(),
    }


def test_browser_interaction_state_contracts():
    import pytest

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for browser interaction contract tests")
    payload = browser_check_payload()
    script = payload["harness"] + "\nrunBrowserInteractionChecks(" + json.dumps(payload["sources"]) + """
    ).then((passed) => console.log(JSON.stringify(passed))).catch((error) => {
      console.error(error);
      process.exitCode = 1;
    });
    """
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(json.loads(result.stdout)) == 7
