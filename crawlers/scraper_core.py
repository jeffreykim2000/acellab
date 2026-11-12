# scraper_core.py
# Generic company-information scraper for any website.
# Extracts: company name, website, founding company, funding, socials,
# description, products, contact, founders, employees, investors, images.
#
# Supports: static HTML (requests + BeautifulSoup) and JS-rendered (Selenium).

import re
import json
import time
from urllib.parse import urljoin, urlparse
from typing import Optional, Dict, List, Any, Callable

# Static parsing
try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

# Selenium (optional)
try:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import TimeoutException, NoSuchElementException
    from webdriver_manager.chrome import ChromeDriverManager
    HAS_SELENIUM = True
except ImportError:
    HAS_SELENIUM = False

# Default user agent
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Social domain patterns -> label
SOCIAL_PATTERNS = [
    (r"linkedin\.com", "LinkedIn"),
    (r"twitter\.com|x\.com", "Twitter/X"),
    (r"instagram\.com", "Instagram"),
    (r"facebook\.com|fb\.com|fb\.me", "Facebook"),
    (r"youtube\.com|youtu\.be", "YouTube"),
    (r"tiktok\.com", "TikTok"),
    (r"github\.com", "GitHub"),
    (r"crunchbase\.com", "Crunchbase"),
    (r"angel\.co|wellfound\.com", "AngelList/Wellfound"),
]

# Email and phone regex
EMAIL_RE = re.compile(
    r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
    re.IGNORECASE
)
PHONE_RE = re.compile(
    r"(?:\+?1[-.\s]?)?\(?[0-9]{3}\)?[-.\s]?[0-9]{3}[-.\s]?[0-9]{4}"
    r"|(?:\+[0-9]{1,3}[-.\s]?)?[0-9]{2,4}[-.\s]?[0-9]{2,4}[-.\s]?[0-9]{2,4}"
)

# Funding-related keywords
FUNDING_KEYWORDS = re.compile(
    r"\b(seed|pre-seed|series\s*a|series\s*b|series\s*c|series\s*d|"
    r"angel|venture|raised|funding|invested|backed)\b",
    re.IGNORECASE
)
DOLLAR_AMOUNT = re.compile(r"\$[\d,]+(?:\.\d+)?\s*(?:M|K|B|million|billion)?", re.IGNORECASE)


def _label_social(href: str) -> Optional[str]:
    if not href or not isinstance(href, str):
        return None
    href_lower = href.lower()
    for pattern, label in SOCIAL_PATTERNS:
        if re.search(pattern, href_lower):
            return f"{label}: {href.strip()}"
    return None


def _extract_emails(text: str) -> List[str]:
    return list(dict.fromkeys(EMAIL_RE.findall(text)))


def _extract_phones(text: str) -> List[str]:
    return list(dict.fromkeys(PHONE_RE.findall(text)))


def _extract_schema_org(soup: BeautifulSoup, base_url: str) -> Dict[str, Any]:
    """Extract Organization/Company from JSON-LD and meta."""
    out = {}
    try:
        for script in soup.find_all("script", type="application/ld+json"):
            raw = script.string
            if not raw:
                continue
            raw = raw.strip()
            try:
                data = json.loads(raw)
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict):
                            _merge_schema_item(item, out, base_url)
                elif isinstance(data, dict):
                    _merge_schema_item(data, out, base_url)
            except json.JSONDecodeError:
                continue
    except Exception:
        pass
    return out


def _merge_schema_item(item: Dict, out: Dict, base_url: str) -> None:
    t = (item.get("@type") or "").lower()
    if "organization" not in t and "corporation" not in t and "company" not in t:
        return
    if "name" in item and not out.get("company_name"):
        out["company_name"] = item["name"]
    if "description" in item and not out.get("description"):
        out["description"] = item["description"]
    if "url" in item and not out.get("website"):
        u = item["url"]
        out["website"] = u if isinstance(u, str) else (u[0] if u else "")
    if "sameAs" in item:
        same = item["sameAs"]
        if isinstance(same, str):
            same = [same]
        out["same_as"] = [s for s in same if isinstance(s, str)]
    if "founder" in item:
        f = item["founder"]
        if isinstance(f, dict) and f.get("name"):
            out.setdefault("founders", []).append({"name": f.get("name"), "url": f.get("url")})
        elif isinstance(f, list):
            for x in f:
                if isinstance(x, dict) and x.get("name"):
                    out.setdefault("founders", []).append({"name": x.get("name"), "url": x.get("url")})
    if "address" in item and not out.get("address"):
        addr = item["address"]
        if isinstance(addr, dict):
            parts = [
                addr.get("streetAddress"),
                addr.get("addressLocality"),
                addr.get("addressRegion"),
                addr.get("postalCode"),
                addr.get("addressCountry"),
            ]
            out["address"] = ", ".join(p for p in parts if p)
        elif isinstance(addr, str):
            out["address"] = addr
    if "contactPoint" in item:
        cp = item["contactPoint"]
        if isinstance(cp, dict):
            out.setdefault("contact_points", []).append(cp)
        elif isinstance(cp, list):
            out["contact_points"] = [x for x in cp if isinstance(x, dict)]
    if "parentOrganization" in item and not out.get("founding_company"):
        parent = item["parentOrganization"]
        if isinstance(parent, dict) and parent.get("name"):
            out["founding_company"] = parent["name"]
        elif isinstance(parent, str):
            out["founding_company"] = parent


def _extract_meta(soup: BeautifulSoup) -> Dict[str, str]:
    meta = {}
    for tag in soup.find_all("meta", attrs={"content": True}):
        name = (tag.get("name") or tag.get("property") or "").strip().lower()
        content = (tag.get("content") or "").strip()
        if not content:
            continue
        if name in ("og:title", "twitter:title") and not meta.get("title"):
            meta["title"] = content
        if name in ("og:site_name", "og:title") and not meta.get("site_name"):
            meta["site_name"] = content
        if name in ("description", "og:description", "twitter:description") and not meta.get("description"):
            meta["description"] = content
        if name == "og:url" and not meta.get("canonical_url"):
            meta["canonical_url"] = content
        if name == "og:image":
            meta.setdefault("images", []).append(content)
    if "images" in meta and not isinstance(meta["images"], list):
        meta["images"] = [meta["images"]]
    return meta


def _extract_links_social(soup: BeautifulSoup, base_url: str) -> List[str]:
    social_lines = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        if not href or href.startswith("#") or href in seen:
            continue
        full = urljoin(base_url, href)
        label = _label_social(full)
        if label and full not in seen:
            seen.add(full)
            social_lines.append(label)
    return social_lines


def _extract_contact_from_page(soup: BeautifulSoup, text: str) -> Dict[str, List[str]]:
    contact = {"emails": _extract_emails(text), "phones": _extract_phones(text)}
    for a in soup.find_all("a", href=True):
        h = a.get("href", "").strip().lower()
        if h.startswith("mailto:"):
            email = h[7:].split("?")[0].strip()
            if email and email not in contact["emails"]:
                contact["emails"].append(email)
        if "tel:" in h:
            phone = h.replace("tel:", "").strip()
            if phone and phone not in contact["phones"]:
                contact["phones"].append(phone)
    return contact


def _extract_funding_snippets(text: str) -> List[str]:
    snippets = []
    for m in FUNDING_KEYWORDS.finditer(text):
        start = max(0, m.start() - 80)
        end = min(len(text), m.end() + 120)
        snip = text[start:end].replace("\n", " ").strip()
        if snip and snip not in snippets:
            snippets.append(snip)
    return snippets[:20]


def _extract_products_sections(soup: BeautifulSoup) -> List[Dict[str, str]]:
    products = []
    # Headings that often introduce products
    product_headers = re.compile(
        r"product|solution|service|offering|platform|feature",
        re.IGNORECASE
    )
    for tag in soup.find_all(["h1", "h2", "h3", "h4"]):
        if not product_headers.search(tag.get_text()):
            continue
        name = tag.get_text(separator=" ", strip=True)
        desc = ""
        next_el = tag.find_next_sibling()
        if next_el and next_el.name in ("p", "div"):
            desc = next_el.get_text(separator=" ", strip=True)[:500]
        if name:
            products.append({"name": name, "description": desc})
    return products[:30]


def _extract_team_sections(soup: BeautifulSoup) -> List[Dict[str, str]]:
    team = []
    team_headers = re.compile(r"team|leadership|founder|about\s+us|our\s+people", re.IGNORECASE)
    for tag in soup.find_all(["h1", "h2", "h3", "h4", "section", "div"]):
        if tag.name in ("section", "div"):
            if not team_headers.search(tag.get_text()[:200]):
                continue
        else:
            if not team_headers.search(tag.get_text()):
                continue
        # Look for cards: name + title + optional link
        for block in tag.find_all(["div", "article", "li"], recursive=True):
            links = block.find_all("a", href=True)
            linkedin = ""
            for a in links:
                h = (a.get("href") or "").lower()
                if "linkedin" in h:
                    linkedin = a.get("href", "").strip()
                    break
            texts = block.get_text(separator="|", strip=True).split("|")
            name = ""
            title = ""
            for t in texts:
                t = t.strip()
                if not t or len(t) > 80:
                    continue
                if not name and 2 <= len(t.split()) <= 5:
                    name = t
                elif not title and ("," in t or "CEO" in t or "CTO" in t or "Founder" in t):
                    title = t
            if name or title or linkedin:
                team.append({"name": name or "", "title": title or "", "linkedin": linkedin})
    return team[:50]


def _extract_images(soup: BeautifulSoup, base_url: str, limit: int = 50) -> List[str]:
    urls = []
    seen = set()
    for img in soup.find_all("img", src=True):
        src = img.get("src", "").strip()
        if not src or src in seen:
            continue
        full = urljoin(base_url, src)
        if full.startswith("http") and full not in seen:
            seen.add(full)
            urls.append(full)
        if len(urls) >= limit:
            break
    return urls


def fetch_static(url: str, timeout: int = 30, log: Optional[Callable] = None) -> Optional[str]:
    """Fetch page HTML with httpx or requests."""
    if HAS_HTTPX:
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": DEFAULT_UA}) as client:
                r = client.get(url)
                r.raise_for_status()
                return r.text
        except Exception as e:
            if log:
                log(f"Static fetch failed: {e}")
            return None
    try:
        import requests
        r = requests.get(url, timeout=timeout, headers={"User-Agent": DEFAULT_UA})
        r.raise_for_status()
        return r.text
    except Exception as e:
        if log:
            log(f"Static fetch failed: {e}")
        return None


def scrape_page_static(
    url: str,
    html: Optional[str] = None,
    timeout: int = 30,
    log: Optional[Callable] = None,
) -> Dict[str, Any]:
    """Extract company info from HTML (static). If html is None, fetches url."""
    result = {
        "source_url": url,
        "body_html": "",
        "company_name": "",
        "website": "",
        "founding_company": "",
        "funding_rounds": "",
        "funding_snippets": "",
        "investors": "",
        "socials": "",
        "description": "",
        "products_json": "",
        "contact_emails": "",
        "contact_phones": "",
        "contact_address": "",
        "founders_json": "",
        "employees_json": "",
        "images_json": "",
        "no_information": "no information",
    }
    if not HAS_BS4:
        if log:
            log("BeautifulSoup4 required. pip install beautifulsoup4")
        return result

    if html is None:
        html = fetch_static(url, timeout=timeout, log=log)
    if not html:
        return result

    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(separator=" ", strip=True)
    base_url = url

    # Full body HTML (everything between <body> and </body>)
    body_tag = soup.find("body")
    if body_tag is not None:
        result["body_html"] = str(body_tag)
    else:
        # Fallback: extract from raw HTML
        lower_html = html.lower()
        start = lower_html.find("<body")
        end = lower_html.rfind("</body>")
        if start != -1 and end != -1 and end > start:
            result["body_html"] = html[start : end + len("</body>")]

    # Schema.org
    schema = _extract_schema_org(soup, base_url)
    if schema.get("company_name"):
        result["company_name"] = schema["company_name"]
    if schema.get("description"):
        result["description"] = schema["description"]
    if schema.get("website"):
        result["website"] = schema["website"]
    if schema.get("address"):
        result["contact_address"] = schema["address"]
    if schema.get("founders"):
        result["founders_json"] = json.dumps(schema["founders"], ensure_ascii=False)
    if schema.get("same_as"):
        result["socials"] = "; ".join(schema["same_as"])
    if schema.get("founding_company"):
        result["founding_company"] = schema["founding_company"]

    # Meta
    meta = _extract_meta(soup)
    if not result["company_name"] and meta.get("site_name"):
        result["company_name"] = meta["site_name"]
    if not result["company_name"] and meta.get("title"):
        result["company_name"] = meta["title"]
    if not result["description"] and meta.get("description"):
        result["description"] = meta["description"]
    if meta.get("canonical_url") and not result["website"]:
        result["website"] = meta["canonical_url"]

    # Title / first h1
    if not result["company_name"]:
        title_tag = soup.find("title")
        if title_tag and title_tag.string:
            result["company_name"] = title_tag.string.strip()[:200]
    if not result["company_name"]:
        h1 = soup.find("h1")
        if h1:
            result["company_name"] = h1.get_text(separator=" ", strip=True)[:200]
    # Fallback: company name from copyright/footer (e.g. "© Copyright @2025 Acel Lab Limited")
    if not result["company_name"] or result["company_name"] == result["no_information"]:
        copyright_re = re.compile(
            r"(?:©|copyright\s*[©@]?\s*\d{4})\s*(?:\.?\s*)?([A-Za-z0-9][A-Za-z0-9\s&.,'-]{2,80}(?:Limited|Inc|LLC|Ltd|Corp)?)",
            re.IGNORECASE
        )
        for m in copyright_re.finditer(text):
            name = m.group(1).strip()
            if len(name) > 2 and name.lower() not in ("all rights reserved", "the"):
                result["company_name"] = name[:200]
                break
    # Website: use page URL if nothing else
    if not result["website"] or result["website"] == result["no_information"]:
        try:
            parsed = urlparse(base_url)
            if parsed.scheme and parsed.netloc:
                result["website"] = f"{parsed.scheme}://{parsed.netloc}"
        except Exception:
            pass
    # Description: first substantial paragraph if still empty
    if not result["description"] or result["description"] == result["no_information"]:
        for p in soup.find_all("p"):
            t = p.get_text(separator=" ", strip=True)
            if t and len(t) > 80 and len(t) < 2000:
                result["description"] = t[:1500]
                break

    # Social links (all)
    social_lines = _extract_links_social(soup, base_url)
    if social_lines and not result["socials"]:
        result["socials"] = "; ".join(social_lines)
    elif social_lines and result["socials"]:
        result["socials"] = result["socials"] + "; " + "; ".join(social_lines)

    # Contact
    contact = _extract_contact_from_page(soup, text)
    if contact["emails"]:
        result["contact_emails"] = "; ".join(contact["emails"][:20])
    if contact["phones"]:
        result["contact_phones"] = "; ".join(contact["phones"][:10])

    # Funding & investors
    funding_snippets = _extract_funding_snippets(text)
    if funding_snippets:
        result["funding_snippets"] = " | ".join(funding_snippets[:15])
        result["funding_rounds"] = " (see snippets)"
    # Investor-like phrases (backed by X, invested by Y)
    investor_re = re.compile(
        r"(?:backed|funded|invested)\s+by\s+([^.|]+)|"
        r"(?:investors?|funded by):\s*([^.|]+)",
        re.IGNORECASE
    )
    investor_matches = investor_re.findall(text)
    investor_names = []
    for m in investor_matches:
        for g in m:
            if g:
                t = g.strip()[:150]
                if t and t not in investor_names:
                    investor_names.append(t)
    if investor_names:
        result["investors"] = "; ".join(investor_names[:20])

    # Products
    products = _extract_products_sections(soup)
    if products:
        result["products_json"] = json.dumps(products, ensure_ascii=False)

    # Team / founders / employees
    team = _extract_team_sections(soup)
    if team and not result["founders_json"]:
        result["employees_json"] = json.dumps(team, ensure_ascii=False)

    # Images
    images = _extract_images(soup, base_url)
    if images:
        result["images_json"] = json.dumps(images, ensure_ascii=False)
    if meta.get("images"):
        existing = []
        if result["images_json"] and result["images_json"] != result["no_information"]:
            try:
                existing = json.loads(result["images_json"])
            except Exception:
                pass
        for im in meta["images"]:
            if im not in existing:
                existing.insert(0, im)
        result["images_json"] = json.dumps(existing[:60], ensure_ascii=False)

    # Clean "no information" placeholders
    for k, v in list(result.items()):
        if k == "no_information":
            continue
        if v == "" or v is None:
            result[k] = result["no_information"]
    return result


def get_selenium_driver(headless: bool = False, log: Optional[Callable] = None):
    """Create Chrome WebDriver."""
    if not HAS_SELENIUM:
        if log:
            log("Selenium and webdriver-manager required. pip install selenium webdriver-manager")
        return None
    options = Options()
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1400,2200")
    options.add_argument(f"user-agent={DEFAULT_UA}")
    if headless:
        options.add_argument("--headless=new")
    try:
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
        driver.set_page_load_timeout(60)
        return driver
    except Exception as e:
        if log:
            log(f"Chrome driver failed: {e}")
        return None


def _selenium_wait_with_live_preview(
    driver,
    wait_seconds: float,
    preview_cb: Callable[[str], None],
    log: Optional[Callable] = None,
) -> None:
    """
    While waiting for JS to settle, scroll the page and push PNG screenshots (base64)
    so a remote UI can show a 'live computer' view.
    """
    steps = max(5, min(14, int(wait_seconds * 2) + 4))
    sleep_each = max(0.12, float(wait_seconds) / max(steps, 1))
    try:
        total_h = driver.execute_script(
            "return Math.max(document.body.scrollHeight||0, document.documentElement.scrollHeight||0);"
        )
        vp_h = driver.execute_script("return window.innerHeight||0;")
        total_h = int(total_h or 2400)
        vp_h = int(vp_h or 900)
    except Exception:
        total_h, vp_h = 2400, 900
    max_scroll = max(0, total_h - vp_h)
    for step in range(steps + 1):
        try:
            preview_cb(driver.get_screenshot_as_base64())
        except Exception as e:
            if log:
                log(f"  Live preview frame skipped: {e}")
        if step >= steps:
            break
        try:
            y = int((step + 1) * max_scroll / steps) if max_scroll else 0
            driver.execute_script("window.scrollTo(0, arguments[0]);", y)
        except Exception:
            pass
        time.sleep(sleep_each)
    try:
        driver.execute_script("window.scrollTo(0, 0);")
    except Exception:
        pass


def scrape_page_selenium(
    driver,
    url: str,
    wait_seconds: float = 2.0,
    log: Optional[Callable] = None,
    preview_cb: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Load url in Selenium, get page_source, then run static extractor."""
    try:
        driver.get(url)
    except Exception as e:
        if log:
            log(f"Selenium get failed: {e}")
        return scrape_page_static(url, html=None, log=log)
    if preview_cb:
        if log:
            log("  Live view: capturing page while scrolling…")
        _selenium_wait_with_live_preview(driver, wait_seconds, preview_cb, log=log)
    else:
        time.sleep(wait_seconds)
    html = driver.page_source
    return scrape_page_static(url, html=html, log=log)


def close_popups_selenium(driver, log: Optional[Callable] = None) -> None:
    """Try to close cookie/modal popups."""
    if not HAS_SELENIUM:
        return
    try:
        close_selectors = [
            "button[aria-label='Close']",
            "[data-dismiss='modal']",
            ".modal .close",
            ".cookie-accept",
            "button:contains('Accept')",
            "button:contains('OK')",
        ]
        for sel in close_selectors:
            try:
                els = driver.find_elements(By.CSS_SELECTOR, sel)
                for el in els:
                    if el.is_displayed():
                        el.click()
                        time.sleep(0.3)
                        if log:
                            log("Closed popup.")
                        return
            except Exception:
                continue
    except Exception:
        pass
