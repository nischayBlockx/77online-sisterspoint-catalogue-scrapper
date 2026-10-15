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
        self._setup_headers()
        return True

    def set_cookie_string(self, cookie_str: str):
        for part in cookie_str.split(";"):
            part = part.strip()
            if "=" in part:
                name, value = part.split("=", 1)
                self._session.cookies.set(name.strip(), value.strip(), domain=".bestseller.com")
        log.info("Set %d cookies from string", len(self._session.cookies))
        self._setup_headers()

    def _setup_headers(self):
        csrf = self._session.cookies.get("CSRF-TOKEN") or ""
        sid = self._session.cookies.get("sessionId") or ""
        last_tab = self._session.cookies.get("sessionIdToUseForNewTab") or sid
        self._session.headers.update({
            "__RequestVerificationToken": csrf,
            "tabId": sid,
            "lastTabId": last_tab,
        })

    def is_logged_in(self) -> bool:
        try:
            resp = self._session.get(f"{BASE}/api/user/me", timeout=15)
            if resp.status_code == 200 and "application/json" in resp.headers.get("content-type", ""):
                data = resp.json()
                if data and (data.get("email") or data.get("capabilities")):
                    log.info("Logged in (capabilities: %s)", data.get("capabilities", [])[:3])
                    return True
        except Exception as e:
            log.warning("Auth check failed: %s", e)
        return False

    def get_segmentation_id(self) -> int:
        resp = self._session.get(f"{BASE}/api/segmentation", timeout=15)
        if resp.status_code == 200 and "json" in resp.headers.get("content-type", ""):
            return resp.json().get("id", 55)
        return 55

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

    def get_favourite_list_index(self, list_id: str, seg_id: int = 55) -> dict | None:
        return self.api_post("/api/favouritelist/index", {
            "segmentationId": seg_id,
            "filters": [],
            "groupingType": "Ungrouped",
            "favouriteListId": list_id,
            "sortBy": "MasterIdOrder",
            "sortDirection": "Descending",
        })

    def get_favourite_list_page(self, list_id: str, page: int = 1,
                                page_size: int = 36, seg_id: int = 55) -> dict | None:
        return self.api_post("/api/favouritelist/page", {
            "segmentationId": seg_id,
            "sortBy": "MasterIdOrder",
            "sortDirection": "Descending",
            "pageNumber": page,
            "pageSize": page_size,
            "filters": [],
            "favouriteListId": list_id,
            "groupingType": "Ungrouped",
            "groupingKey": "",
        })

    def get_all_favourite_products(self, list_id: str) -> list[dict]:
        seg_id = self.get_segmentation_id()
        all_items = []
        page = 1
        while True:
            log.info("Fetching favourite list page %d...", page)
            data = self.get_favourite_list_page(list_id, page=page, page_size=100, seg_id=seg_id)
            if not data:
                break
            lines = data.get("lines", [])
            if not lines:
                log.info("No more items on page %d", page)
                break
            all_items.extend(lines)
            paging = data.get("pagingInformation", {})
            total = paging.get("totalNumberOfItems", 0)
            log.info("Page %d: %d items (total so far: %d / %d)", page, len(lines), len(all_items), total)
            if total and len(all_items) >= total:
                break
            if len(lines) < 100:
                break
            page += 1
        return all_items
