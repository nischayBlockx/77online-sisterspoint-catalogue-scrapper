"""Bestseller MORE B2B scraper — uses Playwright for auth, REST API for data."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright, Page, BrowserContext

log = logging.getLogger(__name__)

BASE = "https://more.bestseller.com"
AUTH_STATE = Path(".auth/bestseller_state.json")


class BestsellerScraper:
    def __init__(self, headless: bool = True, timeout: int = 30_000):
        self.headless = headless
        self.timeout = timeout
        self._pw = None
        self._browser = None
        self._ctx: BrowserContext | None = None
        self._page: Page | None = None

    def __enter__(self):
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        if AUTH_STATE.exists():
            self._ctx = self._browser.new_context(storage_state=str(AUTH_STATE))
            log.info("Loaded saved session from %s", AUTH_STATE)
        else:
            self._ctx = self._browser.new_context()
        self._page = self._ctx.new_page()
        self._page.set_default_timeout(self.timeout)
        return self

    def __exit__(self, *exc):
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()

    @property
    def page(self) -> Page:
        return self._page

    def _save_state(self):
        AUTH_STATE.parent.mkdir(parents=True, exist_ok=True)
        self._ctx.storage_state(path=str(AUTH_STATE))
        log.info("Session saved to %s", AUTH_STATE)

    def login_interactive(self) -> bool:
        """Open browser for manual OTP login. Returns True on success."""
        self._page.goto(f"{BASE}/en/prelogin")
        print("\n=== Please log in manually in the browser window ===")
        print("Complete OTP verification, then the scraper will continue.\n")
        try:
            self._page.wait_for_url("**/en/**", timeout=300_000)
            # Wait until we're past prelogin
            for _ in range(60):
                if "/prelogin" not in self._page.url:
                    break
                time.sleep(2)
            else:
                log.error("Login timed out")
                return False
        except Exception as e:
            log.error("Login failed: %s", e)
            return False

        self._save_state()
        log.info("Login successful: %s", self._page.url)
        return True

    def is_logged_in(self) -> bool:
        resp = self._page.goto(f"{BASE}/api/user/me")
        if resp and resp.status == 200:
            ct = resp.headers.get("content-type", "")
            if "text/html" in ct:
                return False
            try:
                data = resp.json()
                if data and data.get("email"):
                    log.info("Logged in as %s", data["email"])
                    return True
            except Exception:
                pass
        return False

    def ensure_auth(self) -> bool:
        if self.is_logged_in():
            return True
        log.info("Session expired, need to re-login")
        # Clear old state and try interactive
        if AUTH_STATE.exists():
            AUTH_STATE.unlink()
        self._ctx.clear_cookies()
        return self.login_interactive()

    def api_get(self, path: str, params: dict | None = None) -> Any:
        url = f"{BASE}{path}"
        if params:
            qs = "&".join(f"{k}={v}" for k, v in params.items())
            url = f"{url}?{qs}"
        resp = self._page.goto(url)
        if not resp or resp.status != 200:
            log.warning("API %s returned %s", path, resp.status if resp else "None")
            return None
        ct = resp.headers.get("content-type", "")
        if "text/html" in ct:
            log.warning("API %s returned HTML (not authenticated?)", path)
            return None
        try:
            return resp.json()
        except Exception:
            log.warning("API %s returned non-JSON response", path)
            return None

    def api_post(self, path: str, body: dict) -> Any:
        url = f"{BASE}{path}"
        resp = self._page.evaluate("""
            async ([url, body]) => {
                const r = await fetch(url, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(body),
                    credentials: 'include'
                });
                return {status: r.status, data: await r.json()};
            }
        """, [url, body])
        if resp["status"] != 200:
            log.warning("POST %s returned %s", path, resp["status"])
            return None
        return resp["data"]

    def get_favourite_list(self, list_id: str) -> dict | None:
        return self.api_get("/api/favouritelist/index", {"favouriteListId": list_id})

    def get_favourite_list_page(self, list_id: str, page: int = 0, page_size: int = 100) -> dict | None:
        return self.api_get("/api/favouritelist/page", {
            "favouriteListId": list_id,
            "page": str(page),
            "pageSize": str(page_size),
        })

    def get_all_favourite_products(self, list_id: str) -> list[dict]:
        all_items = []
        page = 0
        while True:
            log.info("Fetching favourite list page %d...", page)
            data = self.get_favourite_list_page(list_id, page=page, page_size=100)
            if not data:
                break
            items = data.get("items") or data.get("products") or data.get("families") or []
            if not items:
                # Try to find the items in the response
                for key in data:
                    if isinstance(data[key], list) and len(data[key]) > 0:
                        items = data[key]
                        log.info("Found items under key '%s'", key)
                        break
            if not items:
                log.info("No more items on page %d", page)
                break
            all_items.extend(items)
            log.info("Page %d: %d items (total: %d)", page, len(items), len(all_items))
            total = data.get("totalCount") or data.get("total") or data.get("totalItems")
            if total and len(all_items) >= total:
                break
            if len(items) < 100:
                break
            page += 1
        return all_items

    def get_product_detail(self, family_id: str) -> dict | None:
        return self.api_get(f"/api/product/family/{family_id}")

    def search_products(self, query: str = "", page: int = 0, page_size: int = 50) -> dict | None:
        return self.api_post("/api/product/search", {
            "query": query,
            "page": page,
            "pageSize": page_size,
        })

    def export_favourite_list_excel(self, list_id: str, out_path: Path) -> bool:
        """Download the platform's own Excel export for a favourite list."""
        data = self._page.evaluate("""
            async ([url]) => {
                const r = await fetch(url, {credentials: 'include'});
                if (!r.ok) return {status: r.status, ok: false};
                const buf = await r.arrayBuffer();
                const bytes = Array.from(new Uint8Array(buf));
                return {status: r.status, ok: true, bytes: bytes};
            }
        """, [f"{BASE}/api/favouritelistexport/exportasexcel?favouriteListId={list_id}"])
        if not data or not data.get("ok"):
            log.warning("Excel export returned %s", data.get("status") if data else "None")
            return False
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(bytes(data["bytes"]))
        log.info("Excel export saved to %s (%d bytes)", out_path, len(data["bytes"]))
        return True

    def discover(self, list_id: str, out_dir: Path):
        """Save raw API responses for debugging."""
        out_dir.mkdir(parents=True, exist_ok=True)

        log.info("Discovering favourite list %s...", list_id)

        # Get list info
        info = self.get_favourite_list(list_id)
        if info:
            (out_dir / "01_list_info.json").write_text(json.dumps(info, indent=2, ensure_ascii=False))
            log.info("List info saved")

        # Get first page of products
        page_data = self.get_favourite_list_page(list_id, page=0, page_size=20)
        if page_data:
            (out_dir / "02_list_page0.json").write_text(json.dumps(page_data, indent=2, ensure_ascii=False))
            log.info("First page saved (%d bytes)", len(json.dumps(page_data)))

        # Navigate to the favourite list page and capture network requests
        log.info("Loading favourite list in browser to capture API calls...")
        responses = []
        def on_response(resp):
            if "/api/" in resp.url and resp.status == 200:
                try:
                    responses.append({"url": resp.url, "status": resp.status, "body": resp.json()})
                except Exception:
                    pass

        self._page.on("response", on_response)
        self._page.goto(f"{BASE}/en/favouritelist?favouriteListId={list_id}")
        self._page.wait_for_load_state("networkidle")
        time.sleep(3)

        if responses:
            (out_dir / "03_api_responses.json").write_text(json.dumps(responses, indent=2, ensure_ascii=False))
            log.info("Captured %d API responses", len(responses))

        # Screenshot
        self._page.screenshot(path=str(out_dir / "04_favourite_list.png"), full_page=True)
        log.info("Screenshot saved")

        # Page HTML
        (out_dir / "05_page.html").write_text(self._page.content())
        log.info("HTML saved")

        self._page.remove_listener("response", on_response)
