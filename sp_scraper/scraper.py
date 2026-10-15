"""Playwright bot: log in, walk categories, open every product, extract data."""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse

from playwright.sync_api import Error as PWError
from playwright.sync_api import Page, Response, sync_playwright
from playwright.sync_api import TimeoutError as PWTimeout

from .exporters import append_product_jsonl, load_products_jsonl
from .extract import extract_product

log = logging.getLogger("sp_scraper")

USERNAME_SELECTORS = [
    "input[type=email]",
    "input[autocomplete=username]",
    "input[name*=user i]", "input[id*=user i]",
    "input[name*=email i]", "input[id*=email i]",
    "input[name*=login i]", "input[id*=login i]",
    "input[name*=customer i]", "input[name*=account i]",
]
PASSWORD_SELECTOR = "input[type=password]"
LOGIN_LINK_RE = re.compile(r"^\s*(log ?in|sign ?in|log ind|logind|anmelden|login)\s*$", re.I)
COOKIE_BUTTON_RE = re.compile(r"^\s*(accept( all)?( cookies)?|allow all|i agree|agree|ok|got it|accepter( alle)?|tillad alle|godkend( alle)?|alle akzeptieren)\s*$", re.I)


class LoginError(RuntimeError):
    pass


class SistersPointScraper:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.sel = cfg["selectors"]
        self.out = Path(cfg["output_dir"])
        self.debug_dir = self.out / "debug"
        self.state_file = Path(".auth/state.json")
        self.base_host = urlparse(cfg["base_url"]).netloc
        self._json_payloads: list = []
        self._pw = self._browser = self._context = None
        self.page: Page | None = None

    # ------------------------------------------------------------------ lifecycle
    def __enter__(self):
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.cfg["headless"])
        kwargs = {"viewport": {"width": 1440, "height": 1000}, "locale": "en-GB"}
        if self.state_file.exists():
            kwargs["storage_state"] = str(self.state_file)
        self._context = self._browser.new_context(**kwargs)
        self._context.set_default_timeout(self.cfg["timeout_seconds"] * 1000)
        self.page = self._context.new_page()
        self.page.on("response", self._on_response)
        return self

    def __exit__(self, *exc):
        for obj in (self._context, self._browser):
            try:
                obj and obj.close()
            except PWError:
                pass
        if self._pw:
            self._pw.stop()

    # ------------------------------------------------------------------ helpers
    def _on_response(self, resp: Response) -> None:
        if not self.cfg.get("capture_json"):
            return
        try:
            ctype = resp.headers.get("content-type", "")
            if "json" not in ctype or resp.request.resource_type not in ("xhr", "fetch"):
                return
            body = resp.body()
            if len(body) < 2_000_000:
                self._json_payloads.append(json.loads(body))
        except Exception:  # noqa: BLE001 - best effort only
            pass

    def goto(self, url: str) -> None:
        log.debug("GET %s", url)
        for attempt in range(3):
            try:
                self.page.goto(url, wait_until="domcontentloaded")
                break
            except PWError as e:
                if attempt == 2:
                    raise
                log.warning("retrying %s (%s)", url, e)
                time.sleep(3 * (attempt + 1))
        try:
            self.page.wait_for_load_state("networkidle", timeout=15000)
        except PWTimeout:
            pass
        time.sleep(self.cfg["delay_seconds"])

    def dump(self, name: str) -> None:
        """Save HTML + screenshot of the current page for debugging selectors."""
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)[:80]
        try:
            (self.debug_dir / f"{safe}.html").write_text(self.page.content(), encoding="utf-8")
            self.page.screenshot(path=str(self.debug_dir / f"{safe}.png"), full_page=True)
        except PWError as e:
            log.warning("could not dump %s: %s", name, e)

    def _first_visible(self, selectors: list[str] | str):
        if isinstance(selectors, str):
            selectors = [selectors]
        for s in selectors:
            if not s:
                continue
            loc = self.page.locator(s)
            for i in range(min(loc.count(), 10)):
                el = loc.nth(i)
                try:
                    if el.is_visible():
                        return el
                except PWError:
                    continue
        return None

    def accept_cookies(self) -> None:
        sel = self.sel["login"].get("cookie_accept")
        el = self._first_visible(sel) if sel else None
        if el is None:
            buttons = self.page.get_by_role("button", name=COOKIE_BUTTON_RE)
            for i in range(min(buttons.count(), 5)):
                if buttons.nth(i).is_visible():
                    el = buttons.nth(i)
                    break
        if el is not None:
            try:
                el.click(timeout=3000)
                time.sleep(0.5)
            except PWError:
                pass

    def same_site(self, url: str) -> bool:
        return urlparse(url).netloc in ("", self.base_host)

    # ------------------------------------------------------------------ login
    def is_logged_in(self) -> bool:
        marker = self.sel["login"].get("logged_in")
        if marker:
            return self._first_visible(marker) is not None
        return self._first_visible(PASSWORD_SELECTOR) is None and self._first_visible(
            "a[href*=logout i], a[href*=log-out i], a[href*=signout i], a[href*=logoff i], "
            "button:has-text('Log out'), a:has-text('Log out'), a:has-text('Log ud'), a:has-text('Logout')"
        ) is not None

    def login(self) -> None:
        cfg = self.cfg
        self.goto(cfg["login_url"])
        self.accept_cookies()
        if self.is_logged_in():
            log.info("Already logged in (saved session).")
            return
        if not cfg["username"] or not cfg["password"]:
            raise LoginError("Set SP_USERNAME and SP_PASSWORD (in .env or the environment).")

        pwd = self._first_visible(self.sel["login"].get("password") or PASSWORD_SELECTOR)
        if pwd is None:
            # Login form may sit behind a "Log in" link / button.
            link = self.page.get_by_role("link", name=LOGIN_LINK_RE).or_(self.page.get_by_role("button", name=LOGIN_LINK_RE))
            if link.count():
                link.first.click()
                try:
                    self.page.wait_for_selector(PASSWORD_SELECTOR, state="visible", timeout=15000)
                except PWTimeout:
                    pass
                self.accept_cookies()
            pwd = self._first_visible(self.sel["login"].get("password") or PASSWORD_SELECTOR)
        if pwd is None:
            self.dump("login_failed_no_form")
            raise LoginError("Could not find the password field. Set selectors.login.* or login_url in config.yaml "
                             f"(page saved to {self.debug_dir}).")

        user_sel = self.sel["login"].get("username")
        user = self._first_visible(user_sel) if user_sel else None
        if user is None:
            # first visible text/email input in the same form as the password field
            form = pwd.locator("xpath=ancestor::form[1]")
            scope = form if form.count() else self.page
            cand = scope.locator("input[type=email], input[type=text], input:not([type])")
            for i in range(cand.count()):
                if cand.nth(i).is_visible():
                    user = cand.nth(i)
                    break
        if user is None:
            user = self._first_visible(USERNAME_SELECTORS)
        if user is None:
            self.dump("login_failed_no_username")
            raise LoginError("Could not find the username/email field; set selectors.login.username.")

        user.fill(cfg["username"])
        pwd.fill(cfg["password"])
        submit = self._first_visible(self.sel["login"].get("submit")) if self.sel["login"].get("submit") else None
        if submit is None:
            form = pwd.locator("xpath=ancestor::form[1]")
            if form.count():
                btn = form.locator("button[type=submit], input[type=submit], button:not([type])")
                submit = btn.first if btn.count() else None
        try:
            with self.page.expect_navigation(timeout=20000):
                submit.click() if submit else pwd.press("Enter")
        except PWTimeout:
            pass  # SPA logins don't navigate
        try:
            self.page.wait_for_load_state("networkidle", timeout=15000)
        except PWTimeout:
            pass
        time.sleep(cfg["delay_seconds"])
        if self._first_visible(PASSWORD_SELECTOR) is not None and not self.is_logged_in():
            self.dump("login_failed")
            raise LoginError(f"Login did not succeed - check credentials. Screenshot saved in {self.debug_dir}.")
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self._context.storage_state(path=str(self.state_file))
        log.info("Logged in as %s", cfg["username"])

    # ------------------------------------------------------------------ categories
    def discover_categories(self) -> list[dict]:
        if self.cfg.get("categories"):
            return [{"name": c["name"], "url": urljoin(self.cfg["base_url"], c["url"])} for c in self.cfg["categories"]]
        self.goto(self.cfg["base_url"])
        links = self.page.eval_on_selector_all(
            self.sel["category_links"],
            "els => els.map(a => ({name: (a.textContent || a.getAttribute('title') || '').replace(/\\s+/g, ' ').trim(), url: a.href}))",
        )
        exclude = re.compile(self.cfg["category_exclude_regex"], re.I) if self.cfg.get("category_exclude_regex") else None
        include = re.compile(self.cfg["category_include_regex"], re.I) if self.cfg.get("category_include_regex") else None
        home = urldefrag(self.cfg["base_url"])[0].rstrip("/")
        cats, seen = [], set()
        for link in links:
            url = urldefrag(link["url"])[0]
            name = link["name"]
            if not name or not url.startswith("http") or not self.same_site(url) or url.rstrip("/") == home:
                continue
            if exclude and (exclude.search(url) or exclude.search(name)):
                continue
            if include and not (include.search(url) or include.search(name)):
                continue
            if url in seen:
                continue
            seen.add(url)
            cats.append({"name": name, "url": url})
        return cats

    # ------------------------------------------------------------------ listings
    _PRODUCT_LINKS_JS = r"""
    (args) => {
      const [sel, rx] = args;
      const re = rx ? new RegExp(rx, 'i') : null;
      let anchors = sel ? [...document.querySelectorAll(sel)] : [...document.querySelectorAll('a[href]')];
      const out = [];
      for (const a of anchors) {
        const href = a.href || (a.querySelector('a[href]') || {}).href;
        if (!href) continue;
        if (re) { if (re.test(href)) out.push(href); continue; }
        if (sel) { out.push(href); continue; }
        if (a.closest('nav, header, footer, [class*=menu], [class*=breadcrumb], [class*=pagination]')) continue;
        // heuristic: link inside a product tile, or a link wrapping a product image
        let el = a, tile = false;
        for (let i = 0; i < 5 && el; i++, el = el.parentElement) {
          const c = ((el.className && el.className.baseVal !== undefined ? el.className.baseVal : el.className) || '') + ' ' + (el.id || '');
          if (/product|item|card|tile|article|style/i.test(c) && el.querySelector('img')) { tile = true; break; }
        }
        if (tile || el?.hasAttribute?.('data-product-id')) out.push(href);
      }
      return out;
    }
    """

    def _product_links_on_page(self, exclude_urls: set[str]) -> list[str]:
        hrefs = self.page.evaluate(self._PRODUCT_LINKS_JS, [self.sel.get("product_links") or "", self.cfg.get("product_url_regex") or ""])
        out = []
        current = urldefrag(self.page.url)[0]
        for h in hrefs:
            u = urldefrag(h)[0]
            if not self.same_site(u) or u == current or u in exclude_urls:
                continue
            if re.search(r"([?&](page|p|sort|order|limit)=|/page/\d+)", u, re.I) and not self.cfg.get("product_url_regex"):
                continue
            if self.cfg.get("category_exclude_regex") and re.search(self.cfg["category_exclude_regex"], u, re.I):
                continue
            out.append(u)
        return list(dict.fromkeys(out))

    def _expand_listing(self) -> None:
        """Click 'load more' / scroll until no new products appear (infinite scroll)."""
        last = -1
        for _ in range(40):
            count = len(self.page.query_selector_all("a[href]"))
            btn = self._first_visible(self.sel.get("load_more") or "")
            if btn is not None:
                try:
                    btn.click(timeout=5000)
                except PWError:
                    break
            else:
                self.page.mouse.wheel(0, 20000)
            time.sleep(max(1.0, self.cfg["delay_seconds"]))
            try:
                self.page.wait_for_load_state("networkidle", timeout=8000)
            except PWTimeout:
                pass
            if count == last and btn is None:
                break
            last = count

    def collect_product_urls(self, category: dict, exclude_urls: set[str]) -> list[str]:
        urls: list[str] = []
        url, visited = category["url"], set()
        limit = int(self.cfg.get("max_products_per_category") or 0)
        for page_no in range(1, int(self.cfg["max_pages_per_category"]) + 1):
            if url in visited:
                break
            visited.add(url)
            self.goto(url)
            self._expand_listing()
            found = self._product_links_on_page(exclude_urls | visited)
            new = [u for u in found if u not in urls]
            urls.extend(new)
            log.info("  %s page %d: %d product links (%d new)", category["name"], page_no, len(found), len(new))
            if page_no == 1 and not found:
                self.dump(f"category_no_products_{category['name']}")
            if limit and len(urls) >= limit:
                return urls[:limit]
            nxt = self.page.eval_on_selector_all(self.sel["next_page"], "els => els.map(e => e.href).filter(Boolean)")
            nxt = [urldefrag(n)[0] for n in nxt if self.same_site(n) and urldefrag(n)[0] not in visited]
            if not nxt or not new:
                break
            url = nxt[0]
        return urls

    # ------------------------------------------------------------------ products
    def scrape_product(self, url: str, category: str):
        self._json_payloads = []
        self.goto(url)
        html = self.page.content()
        product = extract_product(html, self.page.url, self.sel["product"], self._json_payloads, category)
        product.url = url
        extra = []
        if self.sel["product"].get("color_links"):
            extra = self.page.eval_on_selector_all(self.sel["product"]["color_links"], "els => els.map(e => e.href).filter(Boolean)")
        return product, [urldefrag(u)[0] for u in extra if self.same_site(u)]

    def run(self, only: list[str] | None = None, limit: int = 0) -> Path:
        jsonl = self.out / "products.jsonl"
        done = {p.url for p in load_products_jsonl(jsonl)}
        if done:
            log.info("Resuming: %d products already scraped in %s", len(done), jsonl)
        errors = self.out / "errors.log"
        self.login()
        cats = self.discover_categories()
        if only:
            wanted = [o.lower() for o in only]
            cats = [c for c in cats if any(w in c["name"].lower() or w in c["url"].lower() for w in wanted)]
        if not cats:
            self.dump("no_categories")
            raise RuntimeError("No categories found - list them under `categories:` in config.yaml.")
        log.info("Scraping %d categories: %s", len(cats), ", ".join(c["name"] for c in cats))
        cat_urls = {c["url"] for c in cats}
        total = 0
        for cat in cats:
            log.info("Category: %s (%s)", cat["name"], cat["url"])
            queue = self.collect_product_urls(cat, cat_urls)
            if limit:
                queue = queue[:limit]
            i = 0
            while i < len(queue):
                url = queue[i]
                i += 1
                if url in done:
                    continue
                try:
                    product, extra = self.scrape_product(url, cat["name"])
                except Exception as e:  # noqa: BLE001 - keep going, log the failure
                    log.error("    failed %s: %s", url, e)
                    with errors.open("a", encoding="utf-8") as fh:
                        fh.write(f"{cat['name']}\t{url}\t{e}\n")
                    self.dump(f"error_{urlparse(url).path}")
                    continue
                done.add(url)
                append_product_jsonl(jsonl, product)
                total += 1
                log.info("    [%d/%d] %s | %s | %d variants", i, len(queue), product.style_code or "-", product.style_name, len(product.variants))
                for u in extra:
                    if u not in done and u not in queue:
                        queue.append(u)
        log.info("Scraped %d new products -> %s", total, jsonl)
        return jsonl

    def discover(self) -> dict:
        """Log in and save HTML/screenshots of each page type to output/debug/."""
        self.login()
        self.dump("01_after_login")
        report: dict = {"categories": self.discover_categories()}
        self.dump("02_home")
        if report["categories"]:
            cat = report["categories"][0]
            urls = self.collect_product_urls({**cat, "url": cat["url"]}, {c["url"] for c in report["categories"]})
            self.dump("03_category")
            report["first_category"] = cat
            report["product_urls_sample"] = urls[:10]
            report["product_url_count_first_category"] = len(urls)
            if urls:
                product, _ = self.scrape_product(urls[0], cat["name"])
                self.dump("04_product")
                report["product_sample"] = asdict(product)
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        (self.debug_dir / "discover_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        return report
