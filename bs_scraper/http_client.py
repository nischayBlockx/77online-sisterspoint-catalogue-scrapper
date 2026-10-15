"""HTTP-based Bestseller API client — no Playwright needed, just cookies."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger(__name__)

BASE = "https://more.bestseller.com"
COOKIE_FILE = Path(".auth/bestseller_cookies.json")


class BestsellerHTTP:
    def __init__(self):
        self._session = requests.Session()
        self._session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
        })

    def load_cookies(self, path: Path | None = None) -> bool:
        path = path or COOKIE_FILE
        if not path.exists():
            log.error("Cookie file not found: %s", path)
            return False
        data = json.loads(path.read_text())
        if isinstance(data, list):
            for c in data:
                self._session.cookies.set(
                    c["name"], c["value"],
                    domain=c.get("domain", ".bestseller.com"),
                    path=c.get("path", "/"),
                )
        elif isinstance(data, dict):
            for name, value in data.items():
                self._session.cookies.set(name, value, domain=".bestseller.com")
        log.info("Loaded %d cookies from %s", len(self._session.cookies), path)
        return True

    def set_cookie_string(self, cookie_str: str):
        for part in cookie_str.split(";"):
            part = part.strip()
            if "=" in part:
                name, value = part.split("=", 1)
                self._session.cookies.set(name.strip(), value.strip(), domain=".bestseller.com")
        log.info("Set %d cookies from string", len(self._session.cookies))

    def is_logged_in(self) -> bool:
        try:
            resp = self._session.get(f"{BASE}/api/user/me", timeout=15)
            if resp.status_code == 200 and "application/json" in resp.headers.get("content-type", ""):
                data = resp.json()
                if data and data.get("email"):
                    log.info("Logged in as %s", data["email"])
                    return True
        except Exception as e:
            log.warning("Auth check failed: %s", e)
        return False

    def api_get(self, path: str, params: dict | None = None) -> Any:
        url = f"{BASE}{path}"
        resp = self._session.get(url, params=params, timeout=30)
        if resp.status_code != 200:
            log.warning("GET %s returned %s", path, resp.status_code)
            return None
        ct = resp.headers.get("content-type", "")
        if "text/html" in ct:
            log.warning("GET %s returned HTML (not authenticated?)", path)
            return None
        return resp.json()

    def api_post(self, path: str, body: dict) -> Any:
        url = f"{BASE}{path}"
        resp = self._session.post(url, json=body, timeout=30)
        if resp.status_code != 200:
            log.warning("POST %s returned %s", path, resp.status_code)
            return None
        ct = resp.headers.get("content-type", "")
        if "text/html" in ct:
            log.warning("POST %s returned HTML (not authenticated?)", path)
            return None
        return resp.json()

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
