"""Bounded same-origin product title resolution. Never silently choose fuzzy matches."""

from urllib.parse import urlparse, urljoin
import httpx
from .tls import tls_context
from bs4 import BeautifulSoup
from .transport import validate_url


async def resolve_titles(source_url, titles):
    parsed = await validate_url(source_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    urls = (
        [urljoin(base, "/products/info.html")]
        if parsed.hostname.endswith("printograph.com")
        else [source_url]
    )
    candidates = {}
    async with httpx.AsyncClient(
        verify=tls_context(), timeout=30, follow_redirects=False
    ) as client:
        for url in urls:
            response = await client.get(url)
            if response.status_code != 200:
                continue
            soup = BeautifulSoup(response.text, "html.parser")
            for a in soup.select("a[href]"):
                text = a.get_text(" ", strip=True)
                target = urljoin(url, a["href"])
                if urlparse(target).netloc != parsed.netloc or not text:
                    continue
                candidates[(text.casefold(), target)] = {"title": text, "url": target}
    matches = []
    unresolved = []
    for title in titles:
        exact = [
            v for (text, _), v in candidates.items() if text == title.strip().casefold()
        ]
        order = [v for v in exact if v["url"].rstrip("/").endswith("/order")]
        if order:
            exact = order
        unique = {v["url"]: v for v in exact}
        if len(unique) == 1:
            matches.append(next(iter(unique.values())))
        else:
            unresolved.append(
                {
                    "title": title,
                    "candidates": list(unique.values())
                    or [
                        v
                        for (text, _), v in candidates.items()
                        if title.casefold() in text
                    ][:10],
                }
            )
    return {"matches": matches, "unresolved": unresolved}
