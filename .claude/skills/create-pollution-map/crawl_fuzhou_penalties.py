#!/usr/bin/env python3
"""Crawl Fuzhou environmental penalty records from 环境违法曝光台.

Source: https://www.fuzhou.gov.cn/zgfzzt/shbj/xxgk/ztzl/hjwfpgt/
Articles contain penalty content directly in HTML (no attachments).

This crawler:
  1. Discovers article URLs from the exposure platform listing page
  2. Parses HTML to extract enterprise name, penalty date, violation type
  3. Fuzzy-matches enterprise names to YAML enterprises
  4. Updates YAML with penalty_records and re-runs scoring
"""

import argparse
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
import yaml
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(__file__))
from utils import _names_match
from geocode import _names_match_loose


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ENV_EXPOSURE_URL = "https://www.fuzhou.gov.cn/zgfzzt/shbj/xxgk/ztzl/hjwfpgt/"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
REQUEST_DELAY = 0.5


# ---------------------------------------------------------------------------
# URL Discovery
# ---------------------------------------------------------------------------

def fetch_article_urls() -> List[str]:
    """Fetch article URLs from the environmental exposure listing page."""
    urls = []
    try:
        resp = requests.get(
            ENV_EXPOSURE_URL,
            headers={"User-Agent": USER_AGENT},
            timeout=15,
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        # Find links like ./202605/t20260519_5323876.htm
        for a in soup.find_all("a", href=re.compile(r"^\./\d{6}/t\d{8,}_\d+\.htm$")):
            urls.append(urllib.parse.urljoin(ENV_EXPOSURE_URL, a["href"]))
    except Exception as e:
        print(f"WARNING: Failed to fetch listing: {e}")
    return urls


# ---------------------------------------------------------------------------
# HTML Parse
# ---------------------------------------------------------------------------

def parse_penalty_article(html: str, url: str) -> Optional[Dict]:
    """Parse a penalty article HTML page.

    Returns dict with:
      - name: 当事人名称 (enterprise name)
      - title: article title
      - date: penalty date
      - decision_number: 处罚决定书文号
      - violation: violation description
      - source_url: original URL
    """
    soup = BeautifulSoup(html, "html.parser")

    # Get title from meta tag
    title_meta = soup.find("meta", attrs={"name": "ArticleTitle"})
    title = title_meta["content"] if title_meta else ""

    # Extract all paragraph text
    paragraphs = []
    for p in soup.find_all("p"):
        text = p.get_text(strip=True)
        if text:
            paragraphs.append(text)

    full_text = "\n".join(paragraphs)

    # Extract enterprise name
    name = None
    for pattern in [
        r"当事人名称[：:]\s*([^\n]+)",
        r"当事人[：:]\s*([^\n]+)",
        r"被处罚单位[：:]\s*([^\n]+)",
        r"被处罚人[：:]\s*([^\n]+)",
    ]:
        m = re.search(pattern, full_text)
        if m:
            name = m.group(1).strip()
            break

    # If no explicit "当事人", try to extract from title
    if not name and title:
        # Title format: "XX公司责令改正违法行为决定书..."
        m = re.search(r"([^（(]+?(?:公司|企业|厂|场|站|院))", title)
        if m:
            name = m.group(1).strip()

    # Extract decision number
    decision_number = None
    for pattern in [
        r"(闽榕\w*罚决?〔\d{4}〕\d+号?)",
        r"(闽榕\w*责改〔\d{4}〕\d+号?)",
    ]:
        m = re.search(pattern, full_text)
        if m:
            decision_number = m.group(1)
            break
    if not decision_number and title:
        m = re.search(r"(闽榕\w*〔\d{4}〕\d+号?)", title)
        if m:
            decision_number = m.group(1)

    # Extract year from decision number
    year = None
    if decision_number:
        ym = re.search(r"〔(\d{4})〕", decision_number)
        if ym:
            year = int(ym.group(1))

    # Extract specific date
    date_str = None
    for pattern in [
        r"(\d{4})年(\d{1,2})月(\d{1,2})日.*进行检查",
        r"(\d{4})年(\d{1,2})月(\d{1,2})日.*进行调查",
        r"(\d{4})年(\d{1,2})月(\d{1,2})日.*对你",
    ]:
        m = re.search(pattern, full_text)
        if m:
            date_str = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
            break

    # If no specific date, use year from decision number
    if not date_str and year:
        date_str = f"{year}-01-01"

    # Extract violation description
    violation = None
    # Look for the key violation paragraph
    for para in paragraphs:
        if any(kw in para for kw in ["违法行为", "不符合", "未按照", "超标", "违规", "擅自"]):
            if len(para) > 20 and len(para) < 500:
                violation = para
                break

    if not name:
        return None

    return {
        "name": name,
        "title": title,
        "date": date_str,
        "year": year,
        "decision_number": decision_number,
        "violation": violation,
        "source_url": url,
    }


# ---------------------------------------------------------------------------
# Enterprise Matching
# ---------------------------------------------------------------------------

def match_penalty_to_enterprises(
    penalty: Dict, enterprises: List[Dict]
) -> Tuple[Optional[Dict], float]:
    """Match a penalty record to an enterprise in the YAML."""
    penalty_name = penalty["name"]
    best_match = None
    best_score = 0

    for ent in enterprises:
        ent_name = ent.get("name", "")
        if not ent_name:
            continue

        score = 0
        if penalty_name == ent_name:
            score = 100
        elif penalty_name in ent_name or ent_name in penalty_name:
            score = 90
        elif _names_match_loose(penalty_name, ent_name):
            score = 70
        elif _names_match(penalty_name, ent_name):
            score = 60
        else:
            core_penalty = re.sub(r"有限公司|有限责任公司|股份有限公司|公司|集团", "", penalty_name)
            core_ent = re.sub(r"有限公司|有限责任公司|股份有限公司|公司|集团", "", ent_name)
            if core_penalty in core_ent or core_ent in core_penalty:
                score = 50

        if score > best_score:
            best_score = score
            best_match = ent

    return best_match, best_score


# ---------------------------------------------------------------------------
# YAML Update & Scoring
# ---------------------------------------------------------------------------

def update_enterprise_penalties(
    config: Dict, penalties: List[Dict], min_confidence: float = 50
) -> Tuple[int, int, List[Dict]]:
    """Update enterprises in config with matched penalty records."""
    enterprises = config.get("enterprises", [])
    matched = 0
    unmatched = []

    for penalty in penalties:
        ent, score = match_penalty_to_enterprises(penalty, enterprises)
        if ent and score >= min_confidence:
            if "penalty_records" not in ent:
                ent["penalty_records"] = []
            if "penalty_count" not in ent:
                ent["penalty_count"] = 0

            is_duplicate = False
            for rec in ent["penalty_records"]:
                if rec.get("decision_number") == penalty.get("decision_number"):
                    is_duplicate = True
                    break
                if rec.get("name") == penalty["name"] and rec.get("date") == penalty.get("date"):
                    is_duplicate = True
                    break

            if not is_duplicate:
                ent["penalty_records"].append({
                    "name": penalty["name"],
                    "date": penalty.get("date", ""),
                    "violation": penalty.get("violation", ""),
                    "decision_number": penalty.get("decision_number", ""),
                    "source_url": penalty.get("source_url", ""),
                })
                ent["penalty_count"] = len(ent["penalty_records"])
                matched += 1
                print(f"  Matched: {penalty['name']} -> {ent['name']} (score: {score})")
        else:
            unmatched.append(penalty)
            print(f"  Unmatched: {penalty['name']} (best score: {score})")

    return matched, len(unmatched), unmatched


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

MAIN_DISTRICTS = {"鼓楼区", "台江区", "晋安区", "仓山区", "马尾区"}


def is_in_target_district(ent: dict) -> bool:
    """Check if enterprise address/coordinates are in main urban districts."""
    addr = ent.get("address", "")
    return any(d in addr for d in MAIN_DISTRICTS)


def merge_penalty_enterprises(config: Dict, penalties: List[Dict]) -> int:
    """将处罚企业作为新 source_type='penalty' 追加到 YAML enterprises 列表。"""
    enterprises = config.get("enterprises", [])
    added = 0
    for p in penalties:
        # Skip if not in target district
        if not is_in_target_district(p):
            print(f"  Skipped (out of district): {p['name']}")
            continue

        # Skip if already exists (by name)
        exists = any(e.get("name") == p["name"] for e in enterprises)
        if exists:
            continue

        entry = {
            "name": p["name"],
            "address": p.get("address", f"福州{p['name']}"),
            "label": p["name"],
            "data_source": "福州市生态环境局环境违法曝光台",
            "source_type": "penalty",
            "data_date": p.get("date", ""),
            "categories": ["未分类"],
            "penalty_records": [{
                "name": p["name"],
                "date": p.get("date", ""),
                "violation": p.get("violation", ""),
                "decision_number": p.get("decision_number", ""),
                "source_url": p.get("source_url", ""),
            }],
            "penalty_count": 1,
        }
        enterprises.append(entry)
        added += 1
        print(f"  Added penalty enterprise: {p['name']}")
    return added


def main():
    parser = argparse.ArgumentParser(description="Crawl Fuzhou environmental penalties")
    parser.add_argument("-c", "--config", required=True, help="Path to YAML config")
    parser.add_argument("--urls", nargs="+", help="Specific article URLs to process")
    parser.add_argument("--min-confidence", type=int, default=50, help="Minimum match confidence")
    parser.add_argument("--merge", action="store_true", help="Merge unmatched penalties as new enterprises")
    parser.add_argument("--auto-apply", action="store_true", help="Auto-update YAML and re-score")
    parser.add_argument("--dry-run", action="store_true", help="Don't modify YAML")
    args = parser.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    # Step 1: Discover URLs
    article_urls = args.urls or fetch_article_urls()
    article_urls = sorted(set(article_urls))
    print(f"Total article URLs to process: {len(article_urls)}")
    if not article_urls:
        print("No URLs found.")
        return 1

    # Step 2: Parse each article
    all_penalties = []
    for i, url in enumerate(article_urls, 1):
        print(f"\n[{i}/{len(article_urls)}] {url}")
        try:
            resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
            resp.raise_for_status()
            resp.encoding = "utf-8"
        except Exception as e:
            print(f"  ERROR: {e}")
            continue

        penalty = parse_penalty_article(resp.text, url)
        if not penalty:
            print(f"  Could not parse penalty info")
            continue

        all_penalties.append(penalty)
        print(f"  -> {penalty['name']} | {penalty.get('date', 'N/A')} | {penalty.get('decision_number', 'N/A')}")
        time.sleep(REQUEST_DELAY)

    print(f"\n{'='*60}")
    print(f"Parsed {len(all_penalties)} penalty records")
    print("=" * 60)

    if not all_penalties:
        print("No penalty records found.")
        return 0

    # Step 3: Match to enterprises
    print(f"\nMatching to {len(config.get('enterprises', []))} enterprises...")
    matched, unmatched_count, unmatched = update_enterprise_penalties(
        config, all_penalties, min_confidence=args.min_confidence
    )
    print(f"\nMatched: {matched}, Unmatched: {unmatched_count}")

    if unmatched:
        print("\nUnmatched penalties:")
        for p in unmatched:
            print(f"  - {p['name']} ({p.get('date', 'N/A')})")

    # Step 3.5: Merge unmatched as new enterprises
    merged = 0
    if args.merge and unmatched:
        print(f"\nMerging {len(unmatched)} unmatched penalties as new enterprises...")
        merged = merge_penalty_enterprises(config, unmatched)
        print(f"  Added {merged} new penalty enterprises")

    if args.dry_run:
        print("\n(Dry run - not saving)")
        return 0

    # Save updated config
    with open(args.config, "w", encoding="utf-8") as f:
        yaml.dump(config, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
    print(f"\nSaved: {args.config}")

    if args.auto_apply and (matched > 0 or merged > 0):
        from risk_scoring import assign_risk_levels

        print("\nRe-running risk scoring...")
        assign_risk_levels(config.get("enterprises", []), config)

        high = sum(1 for e in config["enterprises"] if e.get("risk_level") == "high")
        medium = sum(1 for e in config["enterprises"] if e.get("risk_level") == "medium")
        low = sum(1 for e in config["enterprises"] if e.get("risk_level") == "low")
        print(f"  -> high: {high}, medium: {medium}, low: {low}")

        with open(args.config, "w", encoding="utf-8") as f:
            yaml.dump(config, f, allow_unicode=True, sort_keys=False, default_flow_style=False)

        # Regenerate map
        from subprocess import run
        print("\nRegenerating map...")
        result = run([sys.executable, "create_map.py", "-c", args.config])
        if result.returncode != 0:
            print("WARNING: Map generation failed")

    return 0


if __name__ == "__main__":
    sys.exit(main())
