"""Read-only public web lookup. No browser profile, cookies, or local URLs."""

from __future__ import annotations

import ipaddress
import re
import socket
import xml.etree.ElementTree as ET
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlencode, urljoin, urlsplit

import aiohttp


def public_url(url: str) -> str:
    parsed = urlsplit(url)
    if len(url) > 2000 or parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Only public HTTP or HTTPS pages are supported")
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 80, 443}
    ):
        raise ValueError("Credentials and non-web ports are not allowed")
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise ValueError("Local network pages are not allowed")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("Private and special network addresses are not allowed")
    return url


class PublicResolver(aiohttp.resolver.DefaultResolver):
    async def resolve(
        self, host: str, port: int = 0, family: int = socket.AF_INET
    ) -> list[dict[str, Any]]:
        answers = await super().resolve(host, port, family)
        # Validate the exact addresses given to the connector, not a separate
        # DNS preflight vulnerable to rebinding. Revalidate every redirect.
        if not answers or any(not ipaddress.ip_address(item["host"]).is_global for item in answers):
            raise OSError("Web lookup refused a non-public DNS address")
        return answers


class PageText(HTMLParser):
    def __init__(self, base_url: str = "", *, link_query: str = "", max_links: int = 40) -> None:
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.parts: list[str] = []
        self.base_url = base_url
        self.link_query, self.max_links = link_query.casefold(), max_links
        self.links: list[dict[str, str]] = []
        self.link_urls: set[str] = set()
        self.anchor: dict[str, str] | None = None
        self.links_truncated = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in {"script", "style", "noscript", "svg"}:
            self.skip += 1
        if tag == "a" and not self.skip and self.base_url:
            self._finish_anchor()
            href = dict(attrs).get("href")
            if not isinstance(href, str) or not href.strip() or href.lstrip().startswith("#"):
                return
            try:
                resolved = public_url(urljoin(self.base_url, href.strip()))
            except ValueError:
                return
            # DNS validation still happens at actual fetch. These are observed
            # candidate URLs, not authority, navigation, or reachability proof.
            self.anchor = {"url": resolved, "title": ""}

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._finish_anchor()
        if tag in {"script", "style", "noscript", "svg"}:
            self.skip = max(0, self.skip - 1)

    def handle_data(self, data: str) -> None:
        if not self.skip and data.strip():
            self.parts.append(data.strip())
            if self.anchor is not None:
                self.anchor["title"] = (self.anchor["title"] + " " + data.strip()).strip()[:200]

    def _finish_anchor(self):
        if self.anchor is None:
            return
        anchor, self.anchor = self.anchor, None
        if (
            self.link_query
            and self.link_query not in (anchor["title"] + " " + anchor["url"]).casefold()
        ):
            return
        if anchor["url"] in self.link_urls:
            return
        if len(self.links) >= self.max_links:
            self.links_truncated = True
            return
        self.link_urls.add(anchor["url"])
        self.links.append(anchor)

    def close(self):
        super().close()
        self._finish_anchor()


async def download(url: str) -> tuple[str, str]:
    connector = aiohttp.TCPConnector(resolver=PublicResolver(), use_dns_cache=False)
    timeout = aiohttp.ClientTimeout(total=12, connect=4, sock_read=5)
    async with aiohttp.ClientSession(
        connector=connector,
        timeout=timeout,
        cookie_jar=aiohttp.DummyCookieJar(),
        trust_env=False,
        auto_decompress=False,
    ) as session:
        for _ in range(4):
            public_url(url)
            async with session.get(
                url,
                allow_redirects=False,
                headers={"User-Agent": "EV-Local-Assistant/1.0", "Accept-Encoding": "identity"},
            ) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    url = urljoin(url, response.headers.get("Location", ""))
                    continue
                response.raise_for_status()
                if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    raise ValueError("Compressed responses are not accepted by the bounded reader")
                content_type = response.headers.get("Content-Type", "").lower()
                if not any(kind in content_type for kind in ("text/", "xml", "json")):
                    raise ValueError("This page is not readable text")
                body = bytearray()
                async for block in response.content.iter_chunked(16384):
                    body.extend(block)
                    if len(body) > 350_000:
                        raise ValueError("This page exceeds the bounded web reader size")
                return url, body.decode("utf-8", errors="replace")
    raise ValueError("Too many redirects")


async def search(query: str) -> dict[str, Any]:
    query = query.strip()
    if not query or len(query) > 300:
        raise ValueError("Search terms must be 1-300 characters")
    url = "https://www.bing.com/search?" + urlencode({"format": "rss", "q": query})
    _, body = await download(url)
    if "<!DOCTYPE" in body.upper() or "<!ENTITY" in body.upper():
        raise ValueError("Unsupported search response")
    root = ET.fromstring(body)
    results = []
    domains = re.findall(r"\bsite:([a-z0-9.-]+)", query.lower())
    terms = set(re.findall(r"[a-z]{4,}", re.sub(r"\bsite:\S+", "", query.lower()))) - {
        "what",
        "when",
        "where",
        "which",
        "with",
        "from",
        "about",
        "please",
        "find",
        "look",
        "search",
        "official",
        "latest",
        "current",
        "does",
        "have",
        "would",
        "could",
    }
    for item in root.findall("./channel/item"):
        link = item.findtext("link", "")
        try:
            public_url(link)
        except ValueError:
            continue
        host = (urlsplit(link).hostname or "").lower()
        if domains and not any(host == domain or host.endswith("." + domain) for domain in domains):
            continue
        title = item.findtext("title", "")[:200]
        excerpt = unescape(re.sub(r"<[^>]+>", " ", item.findtext("description", "")))[:800]
        evidence_terms = set(re.findall(r"[a-z]{4,}", (title + " " + excerpt + " " + link).lower()))
        if terms and not terms.intersection(evidence_terms):
            continue
        results.append({"title": title, "url": link, "excerpt": excerpt})
        if len(results) == 4:
            break
    return {
        "query": query,
        "sources": results,
        "source_kind": "search_snippets",
        "untrusted_external_content": True,
    }


async def fetch(
    url: str, *, link_query: str = "", max_links: int = 40, max_characters: int = 5000
) -> dict[str, Any]:
    if (
        not isinstance(link_query, str)
        or len(link_query) > 100
        or type(max_links) is not int
        or not 0 <= max_links <= 40
        or type(max_characters) is not int
        or not 100 <= max_characters <= 5000
    ):
        raise ValueError("Invalid bounded page-reader options")
    final_url, body = await download(url)
    parser = PageText(final_url, link_query=link_query, max_links=max_links)
    parser.feed(body)
    parser.close()
    text = re.sub(r"\s+", " ", " ".join(parser.parts))
    return {
        "sources": [{"url": final_url, "excerpt": text[:max_characters]}],
        "links": parser.links,
        "links_truncated": parser.links_truncated,
        "links_filtered": bool(link_query),
        "link_query": link_query,
        "text_truncated": len(text) > max_characters,
        "links_followed": 0,
        "source_kind": "fetched_page",
        "untrusted_external_content": True,
    }


def lookup_request(text: str) -> tuple[str, dict[str, str]] | None:
    """Only the current query, never prior dialogue, enters a web request."""
    clean = re.sub(r"^(?:(?:please|can you|could you|would you)\s+)+", "", text.strip(), flags=re.I)
    if re.match(r"^(?:don't|do not|never|explain how|how (?:do|can))\b", clean, re.I):
        return None
    url = re.search(r"https?://[^\s<>]+", clean)
    if url and re.match(r"^(?:read|summarize|look at|check|what (?:does|is))\b", clean, re.I):
        return "web.fetch", {"url": url[0].rstrip(".,!?")}
    explicit = re.fullmatch(
        r"(?:search(?:\s+(?:the\s+)?(?:web|internet|online))?(?:\s+for)?|look\s+up|google)\s+(.+)",
        clean,
        re.I,
    )
    if explicit and not re.search(
        r"\b(?:on|in)\s+(?:spotify|firefox|chrome|my files|downloads)\b", clean, re.I
    ):
        return "web.search", {"query": explicit[1][:300]}
    if re.match(r"^(?:what|who|when|where|which|is|are|has|how)\b", clean, re.I) and re.search(
        r"\b(?:latest|current|today|news|weather|forecast|this week)\b", clean, re.I
    ):
        if not re.search(
            r"\b(?:my|this)\s+(?:current\s+)?(?:computer|pc|laptop|screen|window|cpu|ram|microphone|network)\b",
            clean,
            re.I,
        ):
            return "web.search", {"query": clean[:300]}
    return None
