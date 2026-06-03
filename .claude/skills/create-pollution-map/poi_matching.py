#!/usr/bin/env python3
"""Shared POI matching utilities for address prefill scripts.

Provides:
  - poi_search_candidates: multi-variant POI search via Gaode Maps
  - score_candidate: score a POI candidate against an enterprise name
  - select_best_address: pick best candidate with confidence thresholds
"""

import re
import time
import urllib.parse
from typing import List, Tuple, Optional

import requests

# Keywords to prefer production facilities over offices
PROD_KWS = ["厂", "基地", "园区", "工业区", "厂区", "生产", "制造", "处理厂", "发电"]
OFFICE_KWS = ["大厦", "写字楼", "中心", "商务楼", "公寓", "商业", "综合体"]

# Blacklist POI categories that are clearly not enterprises
BAD_CATEGORIES = ["购物", "地铁", "公交", "景点", "美食", "酒店"]

_DEFAULT_RATE_LIMIT = 0.15


def poi_search_candidates(
    name: str, city: str, district: str, key: str, rate_limit: float = _DEFAULT_RATE_LIMIT
) -> List[dict]:
    """Search POI with multiple query variants via Gaode Maps.

    Returns all candidates without strict name matching.
    """
    queries = []
    # Variant 1: remove parentheses content
    clean = re.sub(r'[（(].*?[）)]', '', name).strip()
    if clean and clean != name:
        queries.append(clean)
    # Variant 2: original name
    queries.append(name)
    # Variant 3: core short name (last 4-8 chars)
    if len(name) > 8:
        queries.append(name[-8:])

    all_candidates = []
    seen_ids = set()

    for q in queries:
        url = (
            f"https://restapi.amap.com/v3/place/text"
            f"?keywords={urllib.parse.quote(q)}"
            f"&city={urllib.parse.quote(city)}"
            f"&offset=20&page=1"
            f"&key={key}"
        )
        try:
            resp = requests.get(url, timeout=10).json()
            if resp.get("status") == "1" and resp.get("pois"):
                for poi in resp["pois"]:
                    poi_id = poi.get("id", "")
                    if poi_id in seen_ids:
                        continue
                    seen_ids.add(poi_id)

                    poi_name = poi.get("name", "")
                    poi_addr = poi.get("address", "").strip()
                    poi_district = poi.get("adname", "")
                    poi_type = poi.get("type", "")

                    if not poi_addr:
                        continue
                    if any(k in poi_type for k in BAD_CATEGORIES):
                        continue

                    all_candidates.append({
                        "name": poi_name,
                        "address": poi_addr,
                        "district": poi_district,
                        "category": poi_type,
                        "location": poi.get("location", ""),
                    })
        except Exception as e:
            print(f"    API error for '{q}': {e}")

        time.sleep(rate_limit)

    return all_candidates


def score_candidate(poi: dict, enterprise_name: str, target_district: str) -> int:
    """Score a POI candidate. Returns 0-100+ score."""
    score = 0
    poi_name = poi["name"]
    poi_addr = poi["address"]
    poi_district = poi["district"]

    # 1. Name similarity (0-40 pts)
    if enterprise_name == poi_name:
        score += 40
    elif enterprise_name in poi_name or poi_name in enterprise_name:
        score += 35
    else:
        # Core keyword overlap — strip suffixes from *copies* so the
        # original poi_name is never mutated across loop iterations.
        ent_core = re.sub(r'[（(].*?[）)]', '', enterprise_name).strip()
        poi_core = poi_name  # fresh local for suffix stripping
        for suffix in ["有限公司", "有限责任公司", "股份有限公司", "公司"]:
            ent_core = ent_core.replace(suffix, "")
            poi_core = poi_core.replace(suffix, "")

        if ent_core in poi_core or poi_core in ent_core:
            score += 25
        elif len(ent_core) >= 4 and any(c in poi_core for c in ent_core[:4]):
            score += 15

    # 2. Address completeness (0-30 pts)
    if re.search(r'\d+号|\d+幢|\d+层|\d+室|\d+-\d+号', poi_addr):
        score += 30
    elif re.search(r'\d+', poi_addr) and len(poi_addr) > 15:
        score += 20
    elif len(poi_addr) > 10:
        score += 10
    else:
        score += 5

    # 3. District consistency (0-20 pts)
    if target_district and poi_district:
        if target_district in poi_district or poi_district in target_district:
            score += 20
        else:
            score -= 10

    # 4. Address type preference (0-15 pts)
    for kw in PROD_KWS:
        if kw in poi_addr:
            score += 15
            break
    else:
        for kw in OFFICE_KWS:
            if kw in poi_addr:
                score -= 10
                break

    return max(0, score)


def select_best_address(
    candidates: List[dict], enterprise_name: str, target_district: str
) -> Tuple[Optional[str], Optional[dict], List[Tuple[int, dict]], str]:
    """Select best address from candidates.

    Returns: (best_address, best_candidate, all_scored, status)
    status: "auto_fill" | "candidate" | "not_found"
    """
    if not candidates:
        return None, None, [], "not_found"

    scored = [(score_candidate(c, enterprise_name, target_district), c) for c in candidates]
    scored.sort(key=lambda x: x[0], reverse=True)

    best_score, best = scored[0]

    # Decision logic
    if best_score >= 60:
        return best["address"], best, scored, "auto_fill"
    elif best_score >= 35 and len(scored) == 1:
        return best["address"], best, scored, "auto_fill"
    elif best_score >= 35 and len(scored) >= 2 and (best_score - scored[1][0]) >= 20:
        return best["address"], best, scored, "auto_fill"
    elif best_score >= 20:
        return None, best, scored, "candidate"
    else:
        return None, best, scored, "not_found"
