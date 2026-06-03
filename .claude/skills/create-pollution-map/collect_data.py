#!/usr/bin/env python3
"""
Environmental data collection: extract enterprise records from government
websites (HTML tables, PDFs) and enrich addresses via Gaode POI search.

This module is designed to be called by auto_pipeline.py or directly
from the CLI. It does NOT perform web searches itself — URLs must be
provided by the caller (e.g. Claude Code using WebSearch MCP tool).

Usage:
    # Extract from HTML table
    python collect_data.py --url https://www.xxx.gov.cn/... --source-type official

    # Extract from PDF
    python collect_data.py --url https://www.xxx.gov.cn/.../file.pdf --source-type official

    # Enrich addresses for extracted enterprises
    python collect_data.py --input ./extracted.json --city 福州 --enrich
"""

import argparse
import io
import json
import os
import re
import sys
import time
import urllib.parse
from dataclasses import dataclass, field, asdict
from typing import List, Optional

import requests
import yaml
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(__file__))
from utils import (
    get_map_key, _names_match, extract_district_hint, district_match,
    reverse_geocode_district, resolve_cache_path, ensure_dir, get_city_from_config,
    _DISTRICT_KEYWORDS,
)

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None

try:
    import easyocr
except ImportError:
    easyocr = None


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class Source:
    url: str
    title: str = ""
    source_type: str = "official"   # official | license | complaint | exposure | eia | monitoring | penalty
    format: str = "html"            # html | pdf


@dataclass
class Enterprise:
    name: str = ""
    address: str = ""
    district: str = ""              # district from source table (e.g. 鼓楼区, 仓山区)
    category: str = ""              # single category from source
    categories: List[str] = field(default_factory=list)
    label: str = ""
    data_source: str = ""
    source_type: str = "official"   # official | license | complaint | exposure | eia | monitoring | penalty
    data_date: str = ""
    lat: Optional[float] = None
    lon: Optional[float] = None
    geocode_level: str = ""
    raw_text: str = ""              # original text for debugging

    # Risk assessment fields (new)
    risk_level: str = ""            # low | medium | high
    risk_score: int = 0             # 0-100
    risk_factors: List[str] = field(default_factory=list)
    penalty_count: int = 0
    complaint_count: int = 0
    monitoring_violations: int = 0
    eia_level: str = ""             # 报告书 | 报告表 | 登记表 | 未知
    # Detailed records with dates (used for "last N years" filtering)
    penalty_records: List[dict] = field(default_factory=list)
    complaint_records: List[dict] = field(default_factory=list)
    monitoring_records: List[dict] = field(default_factory=list)

    def to_dict(self):
        d = asdict(self)
        # Remove None values for cleaner YAML
        return {k: v for k, v in d.items() if v is not None and v != ""}


# ---------------------------------------------------------------------------
# Category inference
# ---------------------------------------------------------------------------

_CATEGORY_KEYWORDS = [
    # More specific categories first to avoid substring false positives
    # e.g., "地下水污染" contains "水污染" but should match "地下水" not "水环境"
    ("地下水", ["地下水", "地下水污染"]),
    ("水环境", ["水环境", "水重点", "废水", "污水", "水污染", "排污单位(水)"]),
    ("大气环境", ["大气环境", "大气", "废气", "大气重点", "大气污染", "排污单位(大气)"]),
    ("土壤污染", ["土壤", "土壤污染", "土壤重点", "污染监管"]),
    ("噪声", ["噪声", "噪音", "声环境"]),
    ("辐射安全", ["辐射", "放射", "核技术", "射线装置", "放射源", "电磁辐射"]),
    ("环境风险", ["环境风险", "风险管控", "风险重点"]),
    ("固废", ["固废", "危险废物", "危废", "污泥"]),
]


def infer_category(text: str) -> str:
    """Infer pollution category from text context."""
    if not text:
        return ""
    text = text.strip()
    for cat, keywords in _CATEGORY_KEYWORDS:
        for kw in keywords:
            if kw in text:
                return cat
    return ""


def _infer_eia_level(text: str) -> str:
    """Infer EIA level (环评等级) from text."""
    if not text:
        return "未知"
    if "报告书" in text:
        return "报告书"
    if "报告表" in text:
        return "报告表"
    if "登记表" in text:
        return "登记表"
    return "未知"


# ---------------------------------------------------------------------------
# OCR helpers for scanned PDFs
# ---------------------------------------------------------------------------

def _group_ocr_by_rows(ocr_result, y_tolerance=25):
    """Group OCR text blocks by row (Y coordinate).

    Args:
        ocr_result: easyocr result list [(bbox, text, confidence), ...]
        y_tolerance: pixel tolerance for considering blocks on the same row

    Returns:
        List of strings, each representing one row of text (left to right).
    """
    rows = {}
    for item in ocr_result:
        bbox, text, conf = item
        y_center = (bbox[0][1] + bbox[2][1]) / 2
        x_center = (bbox[0][0] + bbox[1][0]) / 2

        found_y = None
        for row_y in list(rows.keys()):
            if abs(row_y - y_center) < y_tolerance:
                found_y = row_y
                break
        if found_y is None:
            found_y = y_center
            rows[found_y] = []
        rows[found_y].append((x_center, text, conf))

    # Sort each row by X and join cells into lines
    sorted_rows = []
    for y in sorted(rows.keys()):
        cells = sorted(rows[y], key=lambda x: x[0])
        line_text = " ".join([c[1] for c in cells])
        sorted_rows.append(line_text)
    return sorted_rows


# ---------------------------------------------------------------------------
# DataExtractor
# ---------------------------------------------------------------------------

class DataExtractor:
    """Extract enterprise records from HTML pages or PDFs."""

    def __init__(self, timeout: int = 30):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        })

    # -- HTML extraction --------------------------------------------------

    def extract_from_html(self, url: str, source: Source = None) -> List[Enterprise]:
        """Fetch HTML page and extract tables containing enterprise names."""
        try:
            resp = self.session.get(url, timeout=self.timeout)
            resp.encoding = resp.apparent_encoding or "utf-8"
            soup = BeautifulSoup(resp.text, "html.parser")
        except Exception as e:
            print(f"ERROR: Failed to fetch {url}: {e}")
            return []

        enterprises = []

        # Strategy 1: Find tables with enterprise-related headers
        tables = soup.find_all("table")

        # Build a category map for each table by analyzing document text
        table_categories = self._build_table_category_map(soup, tables)

        for tidx, table in enumerate(tables):
            rows = table.find_all("tr")
            if len(rows) < 2:
                continue

            # Try to identify header row and column indices
            header_row = rows[0]
            headers = [th.get_text(strip=True) for th in header_row.find_all(["th", "td"])]

            name_idx = self._find_column_index(headers, ["企业名称", "单位名称", "企业详细名称", "企业", "单位", "名称", "排污单位名称"])
            addr_idx = self._find_column_index(headers, ["地址", "详细地址", "经营地址", "生产地址", "住所"])
            cat_idx = self._find_column_index(headers, ["类别", "要素类别", "监管类别", "行业类别", "要素", "环境要素"])
            district_idx = self._find_column_index(headers, ["区县", "县(市、区)", "县(区)", "区/县", "所在区县", "行政区"])

            if name_idx is None:
                continue

            # Get table-level category (from document context or heading)
            table_category = table_categories.get(tidx, "")

            # Extract data rows
            for row in rows[1:]:
                cells = row.find_all(["td", "th"])
                if len(cells) <= name_idx:
                    continue

                name = self._clean_text(cells[name_idx].get_text())
                if not name or len(name) < 4:
                    continue

                # Skip header-like rows that appear in tbody
                if self._is_header_row(name):
                    continue

                address = ""
                if addr_idx is not None and addr_idx < len(cells):
                    address = self._clean_text(cells[addr_idx].get_text())

                district = ""
                if district_idx is not None and district_idx < len(cells):
                    district = self._clean_text(cells[district_idx].get_text())

                category = ""
                if cat_idx is not None and cat_idx < len(cells):
                    category = infer_category(self._clean_text(cells[cat_idx].get_text()))

                # If no category in table, use table-level category from document context
                if not category:
                    category = table_category

                # Skip rows that are clearly not enterprise names
                if self._is_likely_not_enterprise(name):
                    continue

                ent = Enterprise(
                    name=name,
                    address=address,
                    district=district,
                    category=category,
                    data_source=source.title if source else url,
                    source_type=source.source_type if source else "official",
                    raw_text=name + (" | " + district if district else ""),
                )
                enterprises.append(ent)

        # Strategy 2: If no tables found, look for structured lists
        if not enterprises:
            enterprises = self._extract_from_list_structure(soup, source)

        print(f"HTML extraction: {len(enterprises)} enterprises from {url}")
        return enterprises

    def _find_column_index(self, headers: List[str], keywords: List[str]) -> Optional[int]:
        """Find column index matching any of the keywords."""
        for i, h in enumerate(headers):
            h_clean = h.strip().replace(" ", "").replace("\n", "")
            for kw in keywords:
                if kw in h or kw in h_clean:
                    return i
        return None

    def _clean_text(self, text: str) -> str:
        """Clean extracted text."""
        if not text:
            return ""
        text = text.strip()
        # Remove extra whitespace
        text = re.sub(r"\s+", " ", text)
        return text

    def _is_header_row(self, name: str) -> bool:
        """Check if this looks like a header row that leaked into tbody."""
        header_patterns = ["企业名称", "单位名称", "序号", "名称", "地址", "类别", "备注"]
        return name in header_patterns or len(name) < 3

    def _is_likely_not_enterprise(self, name: str) -> bool:
        """Filter out obvious non-enterprise entries."""
        # Skip pure numbers, single words, or obvious headers
        if re.match(r"^\d+$", name):
            return True
        if len(name) < 4:
            return True
        # Skip if it's just an address without company name
        if "路" in name and "公司" not in name and "厂" not in name and "单位" not in name:
            # Could be just an address, but let's be lenient
            pass
        return False

    def _build_table_category_map(self, soup: BeautifulSoup, tables: list) -> dict:
        """Build a mapping from table index to category by analyzing document text.

        Government pages often have headings like "水环境重点排污单位（251家）"
        before each table. We analyze the full text to find these headings and
        map them to the nearest following table.
        """
        # Get full text and find all category headings with their positions
        full_text = soup.get_text()
        lines = full_text.split("\n")

        # Find category heading lines and their line indices
        category_headings = []  # list of (line_index, category)
        for i, line in enumerate(lines):
            line = line.strip()
            if not line:
                continue
            # Look for patterns like "水环境重点排污单位（251家）"
            # or "大气环境重点排污单位"
            cat = infer_category(line)
            if cat and ("重点" in line or "排污" in line or "监管" in line or "管控" in line):
                category_headings.append((i, cat))

        if not category_headings:
            return {}

        # Alternative approach: walk through all relevant elements in document order
        # and track the last seen category heading before each table.
        all_elements = soup.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'div', 'span', 'table'])

        table_categories = {}
        current_category = ""
        tidx = 0

        for elem in all_elements:
            if elem.name == 'table':
                # Check if this is one of our target tables
                if tidx < len(tables) and elem is tables[tidx]:
                    if current_category:
                        table_categories[tidx] = current_category
                    tidx += 1
            else:
                # Check if this element contains a category heading
                text = elem.get_text(strip=True)
                cat = infer_category(text)
                if cat and ("重点" in text or "排污" in text or "监管" in text or "管控" in text):
                    current_category = cat

        return table_categories

    def _extract_from_list_structure(self, soup: BeautifulSoup, source: Source = None) -> List[Enterprise]:
        """Fallback: extract from list structures (ul/li, p tags with numbers)."""
        enterprises = []

        # Try ordered/unordered lists
        for ul in soup.find_all(["ul", "ol"]):
            for li in ul.find_all("li"):
                text = self._clean_text(li.get_text())
                ent = self._parse_free_text(text, source)
                if ent:
                    enterprises.append(ent)

        # Try paragraphs that look like numbered enterprise lists
        for p in soup.find_all("p"):
            text = self._clean_text(p.get_text())
            # Pattern: "1. 企业名称..." or "（1）企业名称..."
            if re.match(r"^[（(]?\d+[)）]?[、.\s]", text):
                ent = self._parse_free_text(text, source)
                if ent:
                    enterprises.append(ent)

        return enterprises

    def _parse_free_text(self, text: str, source: Source = None) -> Optional[Enterprise]:
        """Try to parse an enterprise name from free text."""
        # Remove leading numbers and punctuation
        text = re.sub(r"^[（(]?\d+[)）]?[、.\s]*", "", text)
        text = text.strip()

        if len(text) < 4:
            return None

        # Look for company name patterns
        # Chinese company names typically contain 公司, 厂, 集团, 中心
        if not re.search(r"[公司厂集团中心院部所店]", text):
            return None

        # Try to split name and address
        name = text
        address = ""

        # Common split patterns
        for sep in ["，地址：", " 地址：", "，位于", " 位于", "，经营地址", " 经营地址"]:
            if sep in text:
                parts = text.split(sep, 1)
                name = parts[0].strip()
                address = parts[1].strip() if len(parts) > 1 else ""
                break

        # Clean up name
        name = name.strip("，、；;.")
        if len(name) < 4:
            return None

        category = infer_category(text)

        return Enterprise(
            name=name,
            address=address,
            category=category,
            data_source=source.title if source else "",
            source_type=source.source_type if source else "official",
            raw_text=text,
        )

    # -- PDF extraction ---------------------------------------------------

    def extract_from_pdf(self, url_or_path: str, source: Source = None) -> List[Enterprise]:
        """Download and extract text from a PDF file."""
        if PdfReader is None:
            print("ERROR: pypdf not installed. Run: pip install pypdf")
            return []

        # Download if URL
        pdf_bytes = None
        if url_or_path.startswith(("http://", "https://")):
            try:
                resp = self.session.get(url_or_path, timeout=self.timeout)
                pdf_bytes = io.BytesIO(resp.content)
            except Exception as e:
                print(f"ERROR: Failed to download PDF {url_or_path}: {e}")
                return []
        else:
            pdf_bytes = url_or_path

        # Extract text
        try:
            reader = PdfReader(pdf_bytes)
            full_text = ""
            for page in reader.pages:
                text = page.extract_text()
                if text:
                    full_text += text + "\n"
        except Exception as e:
            print(f"ERROR: Failed to parse PDF: {e}")
            return []

        if not full_text.strip():
            print("WARNING: PDF appears to be scanned/image-based. pypdf text extraction returned empty.")

            if fitz is None:
                print("ERROR: PyMuPDF (fitz) not installed. Cannot render scanned PDF pages.")
                print("  Install: pip install pymupdf")
                return []

            if easyocr is None:
                print("ERROR: easyocr not installed. Cannot OCR scanned PDF pages.")
                print("  Install: pip install easyocr")
                return []

            print("Attempting OCR fallback (this may take a while)...")
            return self._extract_from_scanned_pdf(pdf_bytes, source)

        return self._extract_from_text(full_text, source, url_or_path)

    def _extract_from_text(self, text: str, source: Source = None, source_url: str = "") -> List[Enterprise]:
        """Extract enterprise records from raw text (from PDF or other sources)."""
        enterprises = []
        lines = text.split("\n")

        # Try to find lines that look like enterprise entries
        # Pattern: numbered list with company names
        for line in lines:
            line = line.strip()
            if not line or len(line) < 4:
                continue

            # Skip obvious non-data lines
            if self._is_noise_line(line):
                continue

            # Try to extract enterprise name
            ent = self._parse_free_text(line, source)
            if ent and ent.name:
                # Avoid duplicates
                if not any(e.name == ent.name for e in enterprises):
                    enterprises.append(ent)

        # Also try table-like structures in the text
        table_ents = self._extract_tables_from_text(text, source)
        for ent in table_ents:
            if not any(e.name == ent.name for e in enterprises):
                enterprises.append(ent)

        print(f"Text extraction: {len(enterprises)} enterprises from {source_url}")
        return enterprises

    def _is_noise_line(self, line: str) -> bool:
        """Check if a line is just noise (headers, footers, page numbers)."""
        noise_patterns = [
            r"^\s*\d+\s*$",           # Just a number
            r"^\s*第\s*\d+\s*页",      # Page number
            r"^\s*附件",               # Appendix
            r"^\s*附表",               # Attached table
            r"^\s*说明",               # Note
            r"^\s*备注",               # Remark
            r"^\s*单位：",             # Unit
            r"^\s*\d{4}年\d{1,2}月",   # Date
        ]
        for pat in noise_patterns:
            if re.match(pat, line):
                return True
        return False

    def _extract_tables_from_text(self, text: str, source: Source = None) -> List[Enterprise]:
        """Try to find table structures in plain text (space/tab aligned)."""
        enterprises = []
        lines = text.split("\n")

        # Look for lines with multiple columns separated by 2+ spaces
        for line in lines:
            # Try tab-separated or multi-space separated
            parts = re.split(r"\t|\s{2,}", line.strip())
            if len(parts) >= 2:
                # First non-empty part might be the name
                for part in parts:
                    part = part.strip()
                    if len(part) >= 4 and re.search(r"[公司厂集团中心院部所店]", part):
                        ent = self._parse_free_text(part, source)
                        if ent:
                            enterprises.append(ent)
                            break

        return enterprises

    def _parse_ocr_table_line(self, line: str, source: Source = None) -> Optional[Enterprise]:
        """Parse a single line of OCR table text into an Enterprise.

        Handles patterns like:
          '201 南安市 913505837549629194 固美金属股份有限公司'
        """
        line = line.strip()
        if not line or len(line) < 4:
            return None

        if self._is_noise_line(line) or self._is_header_row(line):
            return None

        # Skip lines that are just a number or just a code
        if re.match(r'^\d+$', line) or re.match(r'^[A-Z0-9]{18}$', line):
            return None

        # Skip parenthetical continuations like "(北峰污水处理厂"
        if line.startswith('(') or line.startswith('（'):
            if not re.search(r'[公司厂集团中心院部所店]', line):
                return None

        # Pattern 1: 序号 区县 18位代码 名称 (full table row)
        m = re.search(r'([^\s]{2,4}(?:区|县|市))\s+([A-Z0-9]{18})\s+(.+)$', line)
        if m:
            district, code, name = m.groups()
            name = name.strip()
            name = re.sub(r'[\s]+[（(].*?[）)]?\s*$', '', name)
            if len(name) >= 4:
                return Enterprise(
                    name=name,
                    district=district,
                    data_source=source.title if source else "",
                    source_type=source.source_type if source else "official",
                    raw_text=line,
                )

        # Pattern 2: 区县 + 公司名称 (code may be OCR garbled)
        m2 = re.search(r'([^\s]{2,4}(?:区|县|市))\s+([^，。\s]{4,}(?:有限公司|有限责任公司|股份有限公司|公司|集团|厂|中心|院))', line)
        if m2:
            district, name = m2.groups()
            return Enterprise(
                name=name,
                district=district,
                data_source=source.title if source else "",
                source_type=source.source_type if source else "official",
                raw_text=line,
            )

        # Pattern 3: just company name (fallback)
        m3 = re.search(r'([^，。\s]{4,}(?:有限公司|有限责任公司|股份有限公司|公司|集团|厂|中心|院))', line)
        if m3:
            name = m3.group(1)
            district = ""
            for kw in _DISTRICT_KEYWORDS:
                if kw in line:
                    district = kw
                    break
            return Enterprise(
                name=name,
                district=district,
                data_source=source.title if source else "",
                source_type=source.source_type if source else "official",
                raw_text=line,
            )

        return None

    def _extract_from_scanned_pdf(self, pdf_bytes, source: Source = None) -> List[Enterprise]:
        """Extract enterprises from a scanned/image-based PDF using OCR."""
        import tempfile

        print("Initializing OCR engine (first run may download models)...")
        ocr_reader = easyocr.Reader(['ch_sim', 'en'], gpu=False)

        if isinstance(pdf_bytes, str):
            # Local file path
            doc = fitz.open(pdf_bytes)
        else:
            if hasattr(pdf_bytes, 'seek'):
                pdf_bytes.seek(0)
            raw = pdf_bytes.read() if hasattr(pdf_bytes, 'read') else pdf_bytes
            doc = fitz.open(stream=raw, filetype="pdf")

        enterprises = []
        total_pages = len(doc)
        current_category = ""  # Track category across pages in the PDF

        for page_num in range(total_pages):
            print(f"  OCR page {page_num + 1}/{total_pages}...", end="", flush=True)
            page = doc[page_num]

            pix = page.get_pixmap(dpi=300)
            img_bytes = pix.tobytes("png")

            with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as f:
                f.write(img_bytes)
                img_path = f.name

            result = ocr_reader.readtext(img_path, detail=1)
            os.unlink(img_path)

            rows = _group_ocr_by_rows(result)

            # Process rows line-by-line, updating category whenever a heading
            # is encountered. Some pages contain multiple sections (e.g. 噪声
            # followed by 土壤污染), so we must track category per-row rather
            # than per-page.
            page_ents = []
            for line in rows:
                # Check if this line is a category heading
                cat = infer_category(line)
                if cat and ("重点" in line or "排污" in line or "监管" in line or "管控" in line):
                    current_category = cat
                    continue

                ent = self._parse_ocr_table_line(line, source)
                if ent:
                    if current_category and not ent.category:
                        ent.category = current_category
                    page_ents.append(ent)

            # Deduplicate within page
            seen = set()
            unique_page_ents = []
            for ent in page_ents:
                key = ent.name
                if key not in seen:
                    seen.add(key)
                    unique_page_ents.append(ent)

            enterprises.extend(unique_page_ents)
            print(f" {len(unique_page_ents)} enterprises")

        doc.close()
        print(f"OCR extraction complete: {len(enterprises)} enterprises from {total_pages} pages")
        return enterprises

    def extract(self, source: Source) -> List[Enterprise]:
        """Route to appropriate extractor based on source format."""
        if source.format == "pdf":
            return self.extract_from_pdf(source.url, source)
        else:
            return self.extract_from_html(source.url, source)

    def extract_risk_data(self, source: Source) -> List[Enterprise]:
        """Extract from penalty / monitoring / EIA / complaint data sources.

        These sources often have different table structures:
          - Penalty: 企业名称 | 处罚日期 | 违法类型 | 处罚结果
          - Monitoring: 企业名称 | 监测时间 | 污染物 | 超标倍数
          - EIA: 项目名称 | 建设地点 | 环评等级 | 审批日期
          - Complaint: 企业名称 | 投诉时间 | 投诉内容
        """
        enterprises = self.extract_from_html(source.url, source)

        for ent in enterprises:
            if source.source_type == "penalty":
                ent.penalty_count = 1
            elif source.source_type == "complaint":
                ent.complaint_count = 1
            elif source.source_type == "monitoring":
                ent.monitoring_violations = 1
            elif source.source_type == "eia":
                ent.eia_level = _infer_eia_level(ent.raw_text)

        return enterprises


# ---------------------------------------------------------------------------
# AddressEnricher
# ---------------------------------------------------------------------------

class AddressEnricher:
    """Enrich enterprise records with addresses via map POI search."""

    def __init__(self, key: str, city: str = "福州", rate_limit: float = 0.15):
        self.key = key
        self.city = city
        self.rate_limit = rate_limit

    # ------------------------------------------------------------------
    # Smart search query generation based on enterprise type + district
    # ------------------------------------------------------------------

    _TYPE_KEYWORDS = {
        "固废处置": {
            "keywords": ["固废", "危废", "废物处置", "废物处理", "垃圾焚烧"],
            "suffixes": ["处置场", "处理厂", "焚烧厂", "填埋场"],
        },
        "污水处理": {
            "keywords": ["水务", "污水", "水处理", "净水", "排水"],
            "suffixes": ["污水处理厂", "污水厂", "水处理厂", "净水厂"],
        },
        "能源发电": {
            "keywords": ["发电", "能源", "清洁能源", "热电", "光伏", "风电"],
            "suffixes": ["发电厂", "电厂", "电站", "能源基地"],
        },
        "畜牧养殖": {
            "keywords": ["畜牧", "养殖", "农牧", "猪场", "鸡场", "奶牛", "肉牛", "家禽"],
            "suffixes": ["养殖场", "养殖基地", "牧场", "养殖小区"],
        },
        "屠宰": {
            "keywords": ["屠宰"],
            "suffixes": ["屠宰场", "屠宰厂"],
        },
        "医院": {
            "keywords": ["医院", "卫生院", "防治院", "疗养院", "疾控中心"],
            "suffixes": ["院区", "分院"],
        },
        "工厂制造": {
            "keywords": ["机械", "制造", "电子", "纺织", "鞋业", "陶瓷", "建材", "化工", "制药", "食品"],
            "suffixes": ["厂区", "工厂", "工业园", "生产基地"],
        },
    }

    def _detect_enterprise_type(self, name: str) -> tuple:
        """Detect enterprise type from name. Returns (type_label, core_name)."""
        core = name
        for suffix in ["有限公司", "有限责任公司", "股份有限公司", "公司", "集团", "厂"]:
            core = core.replace(suffix, "")
        core = core.strip()
        for type_label, cfg in self._TYPE_KEYWORDS.items():
            for kw in cfg["keywords"]:
                if kw in name:
                    return type_label, core
        return "通用", core

    def _build_search_queries(self, name: str, district: str) -> list:
        """Build POI search queries that include the target district.
        The district from the government roster is the authoritative anchor.
        """
        queries = []
        type_label, core = self._detect_enterprise_type(name)
        cfg = self._TYPE_KEYWORDS.get(type_label, {})

        # Primary: full name + district (most specific)
        queries.append(f"{name} {district}")

        # Type-specific: core + suffix + district
        for suffix in cfg.get("suffixes", []):
            queries.append(f"{core}{suffix} {district}")
            queries.append(f"{name}{suffix} {district}")

        # Fallback: core name + district
        if core != name:
            queries.append(f"{core} {district}")

        # Deduplicate while preserving order
        seen = set()
        unique = []
        for q in queries:
            if q not in seen and len(q) >= 4:
                seen.add(q)
                unique.append(q)
        return unique

    def _reverse_check_district(self, lat: float, lon: float, expected_district: str) -> bool:
        """Reverse geocode to verify coordinate falls in expected district."""
        from utils import reverse_geocode_district, district_match
        try:
            actual = reverse_geocode_district(lat, lon, self.key)
            if not actual:
                return False  # Cannot verify district — unsafe to accept
            if expected_district and not district_match(expected_district, actual):
                return False
            return True
        except Exception:
            return False  # Cannot verify — reject rather than risk cross-district errors

    def _poi_search_gaode(self, q: str, district_filter: str, queries: list) -> tuple:
        """Single Gaode POI query. Returns (poi_name, poi_addr, lon, lat) or Nones."""
        url = (
            f"https://restapi.amap.com/v3/place/text"
            f"?keywords={urllib.parse.quote(q)}"
            f"&city={urllib.parse.quote(self.city)}"
            f"&offset=1&page=1&key={self.key}&extensions=all"
        )
        try:
            resp = requests.get(url, timeout=10).json()
            if resp.get("status") == "1" and resp.get("pois"):
                p = resp["pois"][0]
                poi_name = p.get("name", "")
                addr_raw = p.get("address", "")
                if isinstance(addr_raw, list):
                    poi_addr = addr_raw[0] if addr_raw else ""
                else:
                    poi_addr = str(addr_raw).strip()
                loc = p.get("location", "")
                if not loc or "," not in loc:
                    return None
                lon, lat = map(float, loc.split(","))

                # Name matching
                match_ok = False
                for candidate in queries:
                    clean = candidate.replace(district_filter or "", "").strip() if district_filter else candidate
                    if poi_name and _names_match(clean, poi_name):
                        match_ok = True
                        break
                if not match_ok:
                    return None

                # Cross-district validation
                if district_filter:
                    if not self._reverse_check_district(lat, lon, district_filter):
                        return None
                    time.sleep(self.rate_limit)

                return poi_name, poi_addr, lon, lat
        except Exception:
            pass
        return None

    def _poi_search(self, queries: list, district_filter: str = None) -> tuple:
        """Search POI with a list of candidate queries via Gaode Maps.

        Strategy:
          1. Use district in query keywords to bias results toward target area
          2. Accept results even if address string doesn't contain district
             (industrial POIs often use road names, e.g. "S7021(五虎山大桥)")
          3. Verify with reverse geocoding that the coordinate is in the target district

        Returns (poi_name, poi_addr, lon, lat, matched_query) or Nones.
        """
        for q in queries:
            result = self._poi_search_gaode(q, district_filter, queries)
            if result:
                return (*result, q)
        return None, None, None, None, None

    def enrich(self, enterprises: List[Enterprise]) -> dict:
        """Enrich addresses for all enterprises without a real address.
        Uses the enterprise's district (from government roster) as the
        authoritative geographic anchor.
        Returns enrichment report dict.
        """
        fixed = []
        failed = []
        skipped = []

        for ent in enterprises:
            # Skip if already has a good address
            if ent.address and len(ent.address) > 5:
                from utils import is_pseudo_address
                if not is_pseudo_address(ent.address, ent.name):
                    skipped.append(ent.name)
                    continue

            # The district from the roster is the authoritative anchor
            district = ent.district or extract_district_hint(ent.name) or ""
            if not district:
                failed.append({
                    "name": ent.name,
                    "reason": "无法确定目标区县",
                })
                continue

            # Build smart search queries that include the district
            queries = self._build_search_queries(ent.name, district)

            poi_name, poi_addr, lon, lat, matched_q = self._poi_search(
                queries, district_filter=district
            )
            time.sleep(self.rate_limit)

            if lon is not None and lat is not None:
                # Build full address with city prefix if missing
                new_addr = poi_addr
                if new_addr and self.city not in new_addr:
                    new_addr = f"{self.city}{new_addr}"

                ent.address = new_addr
                ent.lat = lat
                ent.lon = lon
                ent.geocode_level = "POI"
                fixed.append({
                    "name": ent.name,
                    "address": new_addr or f"POI坐标({lat:.5f},{lon:.5f})",
                    "matched_query": matched_q,
                })
            else:
                failed.append({
                    "name": ent.name,
                    "reason": f"POI搜索未找到位于{district}的匹配结果",
                    "tried_queries": queries,
                })

        return {
            "fixed": fixed,
            "failed": failed,
            "skipped": skipped,
        }


# ---------------------------------------------------------------------------
# City-level cache (avoid re-extracting for multiple districts)
# ---------------------------------------------------------------------------

def get_city_cache_path(city: str, year: int, output_dir: str) -> str:
    """Return the path for the city-level enterprise cache file."""
    return os.path.join(output_dir, city, f"{city}_{year}_city_cache.json")


def save_city_cache(city: str, year: int, enterprises: List[Enterprise], output_dir: str) -> str:
    """Save the full city-wide enterprise list to a JSON cache file.

    This allows subsequent runs for different districts of the same city
    to skip re-extraction from the original data source (e.g. OCR on a PDF).
    """
    path = get_city_cache_path(city, year, output_dir)
    ensure_dir(path)
    data = [asdict(ent) for ent in enterprises]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path


def load_city_cache(city: str, year: int, output_dir: str) -> Optional[List[Enterprise]]:
    """Load the city-wide enterprise list from the JSON cache file.

    Returns None if the cache file does not exist.
    """
    path = get_city_cache_path(city, year, output_dir)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return [Enterprise(**item) for item in data]


# ---------------------------------------------------------------------------
# Merge and deduplicate
# ---------------------------------------------------------------------------

def merge_sources(sources_results: dict) -> List[Enterprise]:
    """Merge enterprises from multiple sources, deduplicate by name+district.

    Args:
        sources_results: dict mapping source_type -> list of Enterprise

    Returns:
        Deduplicated list with categories and risk counts merged.
    """
    by_key = {}  # key = (name, district)

    for source_type, enterprises in sources_results.items():
        for ent in enterprises:
            key = (ent.name, ent.district)
            if key not in by_key:
                by_key[key] = ent
            else:
                # Merge categories
                existing = by_key[key]
                if ent.category and ent.category not in existing.categories:
                    existing.categories.append(ent.category)
                # Merge source_type if different
                if ent.source_type != existing.source_type:
                    existing.data_source += f"; {ent.data_source}"

                # NEW: Aggregate risk counts
                existing.penalty_count += ent.penalty_count
                existing.complaint_count += ent.complaint_count
                existing.monitoring_violations += ent.monitoring_violations

                # EIA level: keep highest (报告书 > 报告表 > 登记表 > 未知)
                eia_priority = {"报告书": 3, "报告表": 2, "登记表": 1, "未知": 0, "": 0}
                if eia_priority.get(ent.eia_level, 0) > eia_priority.get(existing.eia_level, 0):
                    existing.eia_level = ent.eia_level

    # Set categories for single-category enterprises
    for ent in by_key.values():
        if not ent.categories and ent.category:
            ent.categories = [ent.category]
        elif not ent.categories:
            ent.categories = ["未分类"]

    # Sort by name for stable output
    return sorted(by_key.values(), key=lambda e: e.name)


# ---------------------------------------------------------------------------
# Config generator
# ---------------------------------------------------------------------------

def generate_config(
    city: str,
    district: str,
    year: int,
    enterprises: List[Enterprise],
    output_dir: str,
    district_parts: List[str] = None,
) -> str:
    """Generate a YAML config file from extracted enterprises.

    Args:
        district_parts: If given, appended to the title in parentheses
                        (e.g. ["玄武","秦淮"] → "主城区(玄武+秦淮)").
    Returns the path to the generated config.
    """
    if district_parts:
        district_label = f"{district}({'+'.join(district_parts)})"
    else:
        district_label = district

    # Load defaults.yaml as the base config so plus-version fields
    # (render_mode, risk_scoring, risk_levels, categories.emoji, etc.)
    # are automatically included.
    skill_dir = os.path.dirname(os.path.abspath(__file__))
    defaults_path = os.path.join(skill_dir, "defaults.yaml")
    if os.path.exists(defaults_path):
        with open(defaults_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
    else:
        config = {}

    # Override with city-specific values
    config["meta"] = {
        "title": f"{city}{district_label}{year}年重点污染源单位地理分布图",
        "subtitle": f"数据来源：{city}生态环境局{year}年度环境监管重点单位名录  |  共{len(enterprises)}家  |  坐标：高德地图",
        "output_path": f"./{city}_{district}_{year}_output.png",
        "dpi": 200,
    }
    config["map"] = {
        "figsize": [18, 14],
        "padding": 500,
        "zoom": 12,
        "basemap_alpha": 0.95,
    }
    config["gaode"] = {
        "key": "",
        "cache_file": f"./geocode_cache_{city}.json",
        "rate_limit": 0.15,
        "city": city,
    }
    config["risk_zones"] = {
        "enabled": True,
        "radius_meters": 1000,
        "fill_color": "#FF4444",
        "fill_alpha": 0.12,
        "edge_color": "#CC0000",
        "edge_width": 0.8,
        "edge_alpha": 0.25,
    }
    config["boundary"] = {"coords": []}
    config["enterprises"] = []

    # Build enterprise entries
    for ent in enterprises:
        entry = {
            "name": ent.name,
            "address": ent.address,
            "label": ent.name,
            "data_source": ent.data_source or f"{city}生态环境局{year}年度环境监管重点单位名录",
            "source_type": ent.source_type,
            "data_date": f"{year}-03",
            "categories": ent.categories,
        }
        # Preserve district for downstream partitioning / validation
        if ent.district:
            entry["district"] = ent.district
        # Only include lat/lon if they were enriched
        if ent.lat is not None and ent.lon is not None:
            entry["lat"] = ent.lat
            entry["lon"] = ent.lon
            entry["geocode_level"] = ent.geocode_level

        # Risk fields (only include when non-default)
        if ent.penalty_count:
            entry["penalty_count"] = ent.penalty_count
        if ent.complaint_count:
            entry["complaint_count"] = ent.complaint_count
        if ent.monitoring_violations:
            entry["monitoring_violations"] = ent.monitoring_violations
        if ent.eia_level:
            entry["eia_level"] = ent.eia_level
        # Detailed records with dates (for "last N years" filtering)
        if ent.penalty_records:
            entry["penalty_records"] = ent.penalty_records
        if ent.complaint_records:
            entry["complaint_records"] = ent.complaint_records
        if ent.monitoring_records:
            entry["monitoring_records"] = ent.monitoring_records
        # Risk assessment results (always include if computed)
        if ent.risk_level:
            entry["risk_level"] = ent.risk_level
            entry["risk_score"] = ent.risk_score
            if ent.risk_factors:
                entry["risk_factors"] = ent.risk_factors

        config["enterprises"].append(entry)

    # Write config
    city_dir = os.path.join(output_dir, city)
    os.makedirs(city_dir, exist_ok=True)
    config_path = os.path.join(city_dir, f"{city}_{district}_{year}.yaml")

    with open(config_path, "w", encoding="utf-8") as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False, width=200)

    return config_path


# ---------------------------------------------------------------------------
# Auto-partition districts (merge small adjacent districts)
# ---------------------------------------------------------------------------

def auto_partition_districts(enterprises: list, min_standalone: int = 20,
                              max_merged: int = 40) -> dict:
    """Auto-partition districts based on enterprise count thresholds.

    Rules:
      1. Districts with >= min_standalone enterprises get their own map.
      2. Districts with < min_standalone are greedily merged into groups,
         each group having <= max_merged enterprises total.

    Args:
        enterprises: List of Enterprise objects or dicts with a 'district' field.
        min_standalone: Minimum count for a district to get its own map.
        max_merged: Maximum total count for a merged group.

    Returns:
        dict: {partition_name: {"parts": [district_names],
                                 "enterprises": [Enterprise/dict, ...]}}
    """
    from collections import defaultdict

    district_counts = defaultdict(int)
    district_ents = defaultdict(list)

    for ent in enterprises:
        if hasattr(ent, 'district'):
            d = ent.district or "未知区"
        else:
            d = ent.get("district", "") or "未知区"
        district_counts[d] += 1
        district_ents[d].append(ent)

    result = {}
    small = []

    # Stable sort by district name
    for d, count in sorted(district_counts.items(), key=lambda x: x[0]):
        if count >= min_standalone:
            result[d] = {"parts": [d], "enterprises": district_ents[d]}
        else:
            small.append((d, count, district_ents[d]))

    # Greedily merge small districts (smallest first to maximize packing)
    small.sort(key=lambda x: x[1])
    current_group = []
    current_count = 0
    current_ents = []

    for d, count, ents in small:
        if not current_group or current_count + count <= max_merged:
            current_group.append(d)
            current_count += count
            current_ents.extend(ents)
        else:
            # Flush current group
            name = "+".join(current_group)
            result[name] = {"parts": list(current_group), "enterprises": current_ents}
            current_group = [d]
            current_count = count
            current_ents = list(ents)

    # Flush remaining
    if current_group:
        name = "+".join(current_group)
        result[name] = {"parts": list(current_group), "enterprises": current_ents}

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Extract environmental enterprise data from URLs")
    parser.add_argument("--url", required=True, help="URL to extract data from")
    parser.add_argument("--source-type", default="official", choices=["official", "license", "complaint", "exposure", "eia", "monitoring", "penalty"])
    parser.add_argument("--format", default="auto", choices=["auto", "html", "pdf"])
    parser.add_argument("--output", "-o", help="Output JSON file for extracted enterprises")
    parser.add_argument("--enrich", action="store_true", help="Enrich addresses via Gaode POI")
    parser.add_argument("--city", default="福州", help="City for address enrichment and POI search")
    parser.add_argument("--key", default="", help="API key (or set GAODE_API_KEY env var)")
    args = parser.parse_args()

    # Detect format
    fmt = args.format
    if fmt == "auto":
        fmt = "pdf" if args.url.lower().endswith(".pdf") else "html"

    source = Source(url=args.url, source_type=args.source_type, format=fmt)

    # Extract
    extractor = DataExtractor()
    enterprises = extractor.extract(source)

    if not enterprises:
        print("No enterprises extracted.")
        return 1

    print(f"\nExtracted {len(enterprises)} enterprises:")
    for i, ent in enumerate(enterprises[:10], 1):
        print(f"  {i}. {ent.name}")
        if ent.address:
            print(f"     地址: {ent.address}")
        if ent.category:
            print(f"     类别: {ent.category}")
    if len(enterprises) > 10:
        print(f"     ... and {len(enterprises) - 10} more")

    # Enrich addresses
    if args.enrich:
        key = args.key or get_map_key()
        if not key:
            print("ERROR: No API key. Set GAODE_API_KEY or pass --key")
            return 1

        enricher = AddressEnricher(key=key, city=args.city)
        report = enricher.enrich(enterprises)

        print(f"\n地址补全结果:")
        print(f"  成功: {len(report['fixed'])} 家")
        print(f"  失败: {len(report['failed'])} 家")
        print(f"  跳过: {len(report['skipped'])} 家")

        if report["failed"]:
            print("\n  补全失败的企业:")
            for item in report["failed"]:
                print(f"    - {item['name']}: {item['reason']}")

    # Save output
    if args.output:
        data = [asdict(ent) for ent in enterprises]
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"\nSaved to {args.output}")

    return 0


if __name__ == "__main__":
    exit(main())
