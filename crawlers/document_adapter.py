"""Extract text from company documents explicitly linked by the official site."""
from __future__ import annotations

from typing import Optional

import io
import json
import sqlite3
import zipfile
from urllib.parse import urlparse
import xml.etree.ElementTree as ET

import requests
import startline_agent  # noqa: F401
from orchestrator import SourceAdapter, ScrapedDoc
from .url_safety import get_with_safe_redirects, same_site

MAX_DOCUMENT_BYTES = 20_000_000


class OfficialDocumentAdapter(SourceAdapter):
    source_type = "official_doc"
    deferred_until_discovery = True

    def __init__(self, db_path: str, max_documents: int = 8):
        self.db_path = db_path
        self.max_documents = max_documents
        self.failures: list[dict[str, str]] = []

    def _urls(self, domain: str, company_id: Optional[str] = None) -> list[str]:
        """Brochure URLs this company's own crawl discovered.

        Scoped by company_id when the caller knows it. Matching on domain
        alone would pool evidence across origins — the zone-file record and
        the directory record for one website are separate research, and a
        document found by one must not seed a fetch for the other.
        """
        db = sqlite3.connect(self.db_path)
        if company_id:
            rows = db.execute(
                "SELECT f.value FROM facts f JOIN verdicts v ON v.fact_id=f.id "
                "WHERE f.company_id=? AND f.field='brochure_urls' "
                "AND v.status IN ('verified','unverified')", (company_id,)).fetchall()
        else:
            rows = db.execute(
                "SELECT f.value FROM facts f JOIN verdicts v ON v.fact_id=f.id "
                "JOIN companies c ON c.id=f.company_id WHERE c.domain=? "
                "AND f.field='brochure_urls' AND v.status IN ('verified','unverified')",
                (domain,),
            ).fetchall()
        db.close()
        found = []
        for row in rows:
            try:
                values = json.loads(row[0])
            except ValueError:
                continue
            for url in values if isinstance(values, list) else []:
                if isinstance(url, str) and same_site(domain, url) and url not in found:
                    found.append(url)
        return found[:self.max_documents]

    def has_sources(self, domain: str, company_id: Optional[str] = None) -> bool:
        return bool(self._urls(domain, company_id))

    @staticmethod
    def _office_text(payload: bytes) -> str:
        parts = []
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            for name in archive.namelist():
                lower = name.lower()
                if not (lower.endswith(".xml") and
                        (lower.startswith("word/") or lower.startswith("ppt/")
                         or lower.startswith("xl/sharedstrings"))):
                    continue
                root = ET.fromstring(archive.read(name))
                parts.extend(node.text for node in root.iter() if node.text)
        return "\n".join(parts)

    @staticmethod
    def _pdf_text(payload: bytes) -> tuple[str, str | None]:
        try:
            from pypdf import PdfReader
        except ImportError:
            return "", "pypdf_not_installed"
        try:
            reader = PdfReader(io.BytesIO(payload))
            text = "\n\n".join((page.extract_text() or "") for page in reader.pages[:100])
        except Exception as exc:
            return "", f"invalid_pdf:{type(exc).__name__}"
        if text.strip():
            return text, None
        # Scanned/image PDFs need OCR. Use it when both the Python packages and
        # the system Tesseract binary are available; otherwise report the gap.
        try:
            import fitz
            import pytesseract
            from PIL import Image
            pdf = fitz.open(stream=payload, filetype="pdf")
            pages = []
            for page_number, page in enumerate(pdf):
                if page_number >= 30:
                    break
                pixmap = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
                image = Image.frombytes("RGB", [pixmap.width, pixmap.height], pixmap.samples)
                pages.append(pytesseract.image_to_string(image))
            text = "\n\n".join(pages)
            return (text, None) if text.strip() else ("", "ocr_returned_no_text")
        except (ImportError, RuntimeError, OSError):
            return "", "ocr_unavailable_for_scanned_pdf"

    def fetch(self, company_name: str, domain: str | None, hints=None,
              company_id: Optional[str] = None) -> list[ScrapedDoc]:
        if not domain:
            return []
        self.failures = []
        session = requests.Session()
        docs = []
        for url in self._urls(domain, company_id):
            try:
                response = get_with_safe_redirects(session, url, timeout=30)
                payload = response.content
                if response.status_code >= 400 or len(payload) > MAX_DOCUMENT_BYTES:
                    continue
                path = urlparse(response.url).path.lower()
                if path.endswith(".pdf"):
                    text, failure = self._pdf_text(payload)
                elif path.endswith((".docx", ".pptx", ".xlsx")):
                    text, failure = self._office_text(payload), None
                else:
                    text, failure = "", "unsupported_document_type"
                if text.strip():
                    content = f"DOCUMENT URL: {response.url}\n\n{text}"
                    docs.append(ScrapedDoc(
                        raw_content=content, source_url=response.url,
                        original_chars=len(content), content_truncated=False))
                elif failure:
                    self.failures.append({"source_url": response.url, "reason": failure})
            except (requests.RequestException, OSError, ValueError, zipfile.BadZipFile) as exc:
                self.failures.append({"source_url": url,
                                      "reason": f"document_error:{type(exc).__name__}"})
                continue
        return docs
