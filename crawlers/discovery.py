"""Persistent-crawl discovery helpers for official company websites."""
from __future__ import annotations

import gzip
import io
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse

import requests
from .url_safety import UnsafeURL, ensure_public_url, get_with_safe_redirects, same_site

USER_AGENT = "StartlineBot/0.1 (+https://startline.vip/bot; company-card indexing)"
TIMEOUT = 15
MAX_SITEMAPS = 40
MAX_URLS = 5000
MAX_SITEMAP_BYTES = 10_000_000
TRACKING_PARAMS = {"fbclid", "gclid", "msclkid", "ref", "source"}


@dataclass
class DiscoveredURL:
    url: str
    discovered_from: str
    method: str
    priority: int


def _priority(url: str, want_fields: list[str] | None = None,
              learned: dict[str, float] | None = None) -> int:
    path = urlparse(url).path.lower()
    score = 1
    groups = {
        "contact_email": ("contact", "support", "about", "imprint", "legal"),
        "contact_phone": ("contact", "support", "about", "location"),
        "legal_name": ("legal", "imprint", "terms", "privacy", "about"),
        "hq_location": ("contact", "location", "office", "about", "imprint"),
        "founded_year": ("about", "history", "story", "company"),
        "products": ("product", "service", "solution", "catalog", "portfolio"),
        "pricing_summary": ("pricing", "plan", "price"),
        "key_people": ("team", "people", "leadership", "founder", "management"),
        "funding_stage": ("investor", "funding", "press", "news", "ir"),
    }
    for field in want_fields or []:
        if any(token in path for token in groups.get(field, ())):
            score += 20
    if any(token in path for token in (
            "about", "company", "contact", "product", "service", "team",
            "pricing", "press", "news", "investor", "legal", "imprint")):
        score += 8
    if re.search(r"/(login|signup|cart|checkout|privacy-policy)(/|$)", path):
        score -= 10
    token = re.sub(r"[^a-z0-9]", "", path.strip("/").split("/")[0]) or "home"
    score += round(float((learned or {}).get(token, 0)))
    return score


def normalize_discovered_url(url: str) -> str:
    parsed = urlparse(url)
    query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS]
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    host = (parsed.hostname or "").lower()
    port = parsed.port
    default_port = ((parsed.scheme.lower() == "http" and port == 80) or
                    (parsed.scheme.lower() == "https" and port == 443))
    netloc = host if port is None or default_port else f"{host}:{port}"
    return parsed._replace(scheme=parsed.scheme.lower(), netloc=netloc, path=path,
                           query=urlencode(query), fragment="").geturl()


def _bounded_gunzip(payload: bytes) -> bytes:
    with gzip.GzipFile(fileobj=io.BytesIO(payload)) as stream:
        expanded = stream.read(MAX_SITEMAP_BYTES + 1)
    if len(expanded) > MAX_SITEMAP_BYTES:
        raise OSError("expanded sitemap exceeds size limit")
    return expanded


def discover_sitemap_urls(base_url: str, want_fields: list[str] | None = None,
                           session: requests.Session | None = None,
                           learned: dict[str, float] | None = None) -> list[DiscoveredURL]:
    """Recursively read robots-declared, indexed, and compressed sitemaps."""
    resolve_dns = session is None
    session = session or requests.Session()
    session.headers.setdefault("User-Agent", USER_AGENT)
    host = urlparse(base_url).netloc.lower()
    ensure_public_url(base_url, resolve=resolve_dns)
    seeds = [urljoin(base_url, "/sitemap.xml"),
             urljoin(base_url, "/sitemap_index.xml")]
    try:
        robots_url = urljoin(base_url, "/robots.txt")
        response = get_with_safe_redirects(session, robots_url, timeout=TIMEOUT,
                                           resolve=resolve_dns)
        if response.status_code == 200:
            seeds = re.findall(r"(?im)^sitemap:\s*(\S+)", response.text) + seeds
    except (requests.RequestException, UnsafeURL):
        pass

    queue = list(dict.fromkeys(seeds))
    seen_maps: set[str] = set()
    found: dict[str, DiscoveredURL] = {}
    while queue and len(seen_maps) < MAX_SITEMAPS and len(found) < MAX_URLS:
        sitemap_url = queue.pop(0)
        if sitemap_url in seen_maps:
            continue
        seen_maps.add(sitemap_url)
        try:
            if not same_site(base_url, sitemap_url):
                continue
            response = get_with_safe_redirects(session, sitemap_url, timeout=TIMEOUT,
                                               resolve=resolve_dns)
            if response.status_code != 200:
                continue
            payload = response.content
            if len(payload) > MAX_SITEMAP_BYTES:
                continue
            if sitemap_url.lower().endswith(".gz") or payload[:2] == b"\x1f\x8b":
                payload = _bounded_gunzip(payload)
            root = ET.fromstring(payload)
        except (requests.RequestException, UnsafeURL, OSError, ET.ParseError):
            continue

        root_name = root.tag.rsplit("}", 1)[-1].lower()
        locations = [((node.text or "").strip()) for node in root.iter()
                     if node.tag.rsplit("}", 1)[-1].lower() == "loc"]
        if root_name == "sitemapindex":
            queue.extend(url for url in locations if url not in seen_maps)
            continue
        for url in locations:
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https") or not same_site(base_url, url):
                continue
            normalized = normalize_discovered_url(url)
            found.setdefault(normalized, DiscoveredURL(
                normalized, sitemap_url, "sitemap",
                _priority(normalized, want_fields, learned)))
            if len(found) >= MAX_URLS:
                break
    return sorted(found.values(), key=lambda item: (-item.priority, item.url))
