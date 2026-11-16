"""Tell flat product shots from model photos for Sisters Point galleries.

Flat shots are photographed on a plain white background (border pixels ~255); model photos are not. The
result is cached so a re-export does not download the pictures again.
"""
from __future__ import annotations

import io
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

log = logging.getLogger("sumo_scraper")


def _border_mean(data: bytes) -> float:
    from PIL import Image

    im = Image.open(io.BytesIO(data)).convert("L")
    w, h = im.size
    m = max(2, int(min(w, h) * 0.04))
    px = im.load()
    vals = [px[x, y] for x in range(0, w, 3) for y in list(range(0, m)) + list(range(h - m, h))]
    vals += [px[x, y] for y in range(0, h, 3) for x in list(range(0, m)) + list(range(w - m, w))]
    return sum(vals) / len(vals)


def classify(urls: list[str], get_bytes, cache_path: Path, workers: int = 8) -> dict[str, str]:
    """{url: 'F' (flat) | 'M' (model)}. get_bytes(url) -> bytes | None; unreadable pictures count as model."""
    cache: dict[str, str] = {}
    if cache_path.exists():
        cache = json.loads(cache_path.read_text())
    todo = [u for u in dict.fromkeys(urls) if u not in cache]

    def one(u: str) -> tuple[str, str]:
        data = get_bytes(u.replace("_900x1300c", "_450x650c")) or get_bytes(u)
        try:
            return u, "F" if data and _border_mean(data) >= 250 else "M"
        except Exception:  # noqa: BLE001
            return u, "M"

    if todo:
        log.info("Classifying %d gallery photos (flat vs model)", len(todo))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for u, k in ex.map(one, todo):
                cache[u] = k
        cache_path.write_text(json.dumps(cache))
    return cache
