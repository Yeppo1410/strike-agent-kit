from __future__ import annotations

from .client import StrikeClient
from .config import Settings
from .guardrails import PolicyStore
from .hyperliquid import HyperliquidFunding
from .journal import Journal
from .mock import MockHyperliquid, MockStrike
from .service import AgentService


def build_service(settings: Settings | None = None) -> AgentService:
    settings = settings or Settings.from_env()
    if settings.is_mock:
        backend = MockStrike()
        feed = MockHyperliquid() if settings.use_hyperliquid else None
        poll = 0.0
    else:
        backend = StrikeClient(settings)
        feed = HyperliquidFunding(settings.hyperliquid_url) if settings.use_hyperliquid else None
        poll = 5.0
    return AgentService(settings, backend, feed, PolicyStore(settings.guardrails_path),
                        Journal(settings.journal_path, settings.state_path), poll_timeout_s=poll)
