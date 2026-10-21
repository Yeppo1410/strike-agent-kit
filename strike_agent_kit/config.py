"""Runtime settings, read from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BASE_URLS = {
    "testnet": "https://api-v2-testnet.strikefinance.org",
    "mainnet": "https://api.strikefinance.org",
}
HYPERLIQUID_INFO_URL = "https://api.hyperliquid.xyz/info"


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE lines). Existing env vars win."""
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.split(" #", 1)[0]  # allow trailing comments: KEY=value  # note
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass
class Settings:
    mode: str  # "mock" | "testnet" | "mainnet"
    base_url: str
    api_public_key: str | None
    api_private_key: str | None
    builder_fee_bps: int | None
    guardrails_path: Path
    journal_path: Path
    state_path: Path
    hyperliquid_url: str
    use_hyperliquid: bool

    @property
    def is_mock(self) -> bool:
        return self.mode == "mock"

    @classmethod
    def from_env(cls, env_file: str | None = None) -> "Settings":
        env_path = Path(env_file or os.environ.get("STRIKE_ENV_FILE", ".env"))
        _load_dotenv(env_path)
        # Relative paths are resolved next to the .env file, so the kit behaves the same
        # no matter which working directory the MCP client starts it in.
        base = env_path.resolve().parent

        def _path(value: str) -> Path:
            pth = Path(value).expanduser()
            return pth if pth.is_absolute() else base / pth

        mode = os.environ.get("STRIKE_MODE", "mock").strip().lower()
        if mode not in ("mock", "testnet", "mainnet"):
            raise ValueError(f"STRIKE_MODE must be mock, testnet or mainnet, got {mode!r}")
        if mode == "mainnet" and os.environ.get("STRIKE_ALLOW_MAINNET") != "1":
            raise ValueError("Mainnet is locked. Set STRIKE_ALLOW_MAINNET=1 if you really mean it.")

        public_key = os.environ.get("STRIKE_API_PUBLIC_KEY") or None
        private_key = os.environ.get("STRIKE_API_PRIVATE_KEY") or None
        if mode != "mock" and not (public_key and private_key):
            raise ValueError(
                "STRIKE_API_PUBLIC_KEY and STRIKE_API_PRIVATE_KEY are required outside mock mode."
            )

        fee = os.environ.get("STRIKE_BUILDER_FEE_BPS")
        data_dir = _path(os.environ.get("STRIKE_DATA_DIR", ".strike-agent"))
        return cls(
            mode=mode,
            base_url=os.environ.get("STRIKE_BASE_URL", BASE_URLS.get(mode, "")),
            api_public_key=public_key.lower() if public_key else None,
            api_private_key=private_key,
            builder_fee_bps=int(fee) if fee not in (None, "") else None,
            guardrails_path=_path(os.environ.get("STRIKE_GUARDRAILS", "guardrails.json")),
            journal_path=data_dir / "journal.jsonl",
            state_path=data_dir / "state.json",
            hyperliquid_url=os.environ.get("HYPERLIQUID_INFO_URL", HYPERLIQUID_INFO_URL),
            use_hyperliquid=os.environ.get("STRIKE_USE_HYPERLIQUID", "1") != "0",
        )
