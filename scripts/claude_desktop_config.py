"""Print the Claude Desktop config entry for this machine (absolute paths filled in).

    python scripts/claude_desktop_config.py

Paste the output into claude_desktop_config.json (Claude Desktop: Settings > Developer > Edit Config),
merging it into an existing "mcpServers" block if there is one, then restart Claude Desktop.
"""

import json
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
entry = {
    "mcpServers": {
        "strike": {
            "command": sys.executable,
            "args": ["-m", "strike_agent_kit.server"],
            "cwd": str(root),
            # Everything else (mode, keys, which guardrails file) is read from .env,
            # so switching testnet -> mainnet never requires touching this config.
            "env": {"STRIKE_ENV_FILE": str(root / ".env"), "PYTHONPATH": str(root)},
        }
    }
}
print(json.dumps(entry, indent=2))
print("\nDashboard: http://127.0.0.1:8787 (starts together with Claude Desktop)", file=sys.stderr)
