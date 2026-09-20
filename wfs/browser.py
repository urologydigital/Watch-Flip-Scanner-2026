"""Optional Playwright adapter (Phase 5.3 §21).

Playwright is NOT a dependency. It is imported lazily inside a function, so the
application launches and every non-browser collector works whether or not it is
installed.

Three rules govern its use:

1. Headless by default.
2. One browser session per evidence run, not per reference.
3. It is never used to defeat anti-bot systems. If a site blocks automated
   access, the correct outcome is BLOCKED and a scan that continues without
   that source — not a workaround.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

INSTALL_HINT = "pip install playwright && playwright install chromium"


def is_available() -> bool:
    """Whether Playwright is importable. Never raises."""
    try:
        import playwright  # noqa: F401
        return True
    except Exception:
        return False


def is_enabled() -> bool:
    return os.getenv("WFS53_BROWSER_ENABLED", "0").strip().lower() in {
        "1", "true", "yes", "on"}


def status() -> dict[str, Any]:
    available = is_available()
    return {
        "installed": available,
        "enabled": is_enabled(),
        "usable": available and is_enabled(),
        "install_hint": None if available else INSTALL_HINT,
        "note": ("Optional. The scanner runs fully without it; browser-based "
                 "collectors simply report UNAVAILABLE."),
    }


@dataclass
class FetchResult:
    ok: bool
    html: str = ""
    error: str | None = None
    blocked: bool = False


class BrowserSession:
    """One browser for a whole evidence run. Context-managed, always closes.

    Used as a no-op when Playwright is absent, so callers need no branching.
    """

    def __init__(self, headless: bool = True, timeout_ms: int = 20000):
        self.headless = headless
        self.timeout_ms = timeout_ms
        self._playwright = None
        self._browser = None
        self.available = False

    def __enter__(self) -> "BrowserSession":
        if not (is_available() and is_enabled()):
            return self
        try:
            from playwright.sync_api import sync_playwright
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(headless=self.headless)
            self.available = True
        except Exception:
            self.close()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        for obj, method in ((self._browser, "close"), (self._playwright, "stop")):
            if obj is not None:
                try:
                    getattr(obj, method)()
                except Exception:
                    pass
        self._browser = self._playwright = None
        self.available = False

    def fetch(self, url: str) -> FetchResult:
        """Fetch one page. Blocking responses are reported, never circumvented."""
        if not self.available:
            return FetchResult(False, error="Browser session unavailable.")
        try:
            page = self._browser.new_page()
            try:
                response = page.goto(url, timeout=self.timeout_ms,
                                     wait_until="domcontentloaded")
                if response is not None and response.status in (401, 403, 429):
                    return FetchResult(False, blocked=True,
                                       error=f"HTTP {response.status} — access "
                                             "refused; not circumvented.")
                return FetchResult(True, html=page.content())
            finally:
                page.close()
        except Exception as exc:
            return FetchResult(False, error=f"{type(exc).__name__}: {exc}")
