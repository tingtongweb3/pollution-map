#!/usr/bin/env python3
"""
Batch geocode enterprise addresses using Gaode Maps API with JSON cache.

Coordinate accuracy strategy (priority order):
  1. Name-based POI search — most accurate, uses Gaode's registered POI database
  2. Address geocoding — fallback when POI not found
  3. Cross-validation — if both methods succeed but differ by >1km, warn user
  4. Fuzzy address rejection — addresses at township-level or vague are flagged

Usage:
    python geocode.py -c config.yaml          # geocode all enterprises
    python geocode.py -c config.yaml --force  # force re-geocode (ignore cache)
"""

import argparse
import json
import math
import os
import re
import sys
import time
import urllib.parse

import requests
import yaml

try:
    from shapely.geometry import Polygon, Point
except ImportError:
    Polygon = None
    Point = None

sys.path.insert(0, os.path.dirname(__file__))
from utils import (
    geocode_address, get_map_key, write_yaml_config, _names_match,
    extract_district_hint, extract_target_district, district_match,
    reverse_geocode_district, resolve_cache_path, ensure_dir,
    load_config_with_defaults, infer_address_from_name,
    get_district_bounds, coord_in_bounds, estimate_city_bounds,
    build_production_queries,
)


def _normalize_name_quanzhou(name: str) -> str:
    """More aggressive name normalization for POI matching."""
    n = name
    # Remove bracketed content
    n = re.sub(r"[（(].*?[）)]", "", n)
    # Strip common administrative prefixes (all cities)
    for prefix in ["福建省", "福建", "泉州市", "泉州", "福州市", "福州",
                   "厦门市", "厦门", "漳州市", "漳州", "莆田市", "莆田",
                   "龙岩市", "龙岩", "三明市", "三明", "南平市", "南平",
                   "宁德市", "宁德",
                   "鼓楼区", "台江区", "仓山区", "晋安区", "马尾区",
                   "长乐市", "长乐区", "长乐", "闽侯县", "闽侯",
                   "连江县", "罗源县", "闽清县", "永泰县", "福清市",
                   "鲤城区", "丰泽区", "洛江区", "泉港区", "晋江市", "晋江",
                   "石狮市", "石狮", "南安市", "南安", "惠安县", "惠安",
                   "安溪县", "安溪", "永春县", "永春", "德化县", "德化",
                   "高新区"]:
        n = n.replace(prefix, "")
    # Strip common suffixes
    for suffix in ["有限公司", "有限责任公司", "公司", "集团", "厂",
                   "分公司", "分厂", "停车场(出入口)", "党支部", "中共"]:
        n = n.replace(suffix, "")
    return n.strip()


def _names_match_loose(a: str, b: str) -> bool:
    """Loose name matching for government-official name vs Gaode POI name.

    Government rosters often use full registered names while Gaode POI uses
    abbreviated/common names. This matcher is more lenient than _names_match.
    """
    import difflib

    # Direct substring (already checked by _names_match, but re-check)
    if len(a) >= 4 and len(b) >= 4 and (a in b or b in a):
        return True

    # Normalize both
    na, nb = _normalize_name_quanzhou(a), _normalize_name_quanzhou(b)
    if not na or not nb:
        return False

    # Exact match after normalization
    if na == nb:
        return True

    # Substring after normalization
    min_core = 3
    if len(na) >= min_core and len(nb) >= min_core and (na in nb or nb in na):
        return True

    # SequenceMatcher ratio — 0.60 avoids false matches on generic suffixes
    # like "环保发展" shared by unrelated companies
    ratio = difflib.SequenceMatcher(None, na, nb).ratio()
    if ratio >= 0.60:
        return True

    return False


def haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _try_poi_single_query(query, key, city, city_bounds, district_bounds,
                          target_district, name_for_match, provider="gaode"):
    """Try one POI query via Gaode Maps and return all valid candidates."""
    url = (
        f"https://restapi.amap.com/v3/place/text"
        f"?keywords={urllib.parse.quote(query)}"
        f"&city={urllib.parse.quote(city)}"
        f"&offset=5&page=1&key={key}&extensions=all"
    )
    candidates = []
    try:
        resp = requests.get(url, timeout=10).json()
        if resp.get("status") == "1" and resp.get("pois"):
            for p in resp["pois"]:
                poi_name = p.get("name", "")
                addr_raw = p.get("address", "")
                if isinstance(addr_raw, list):
                    poi_addr = addr_raw[0] if addr_raw else ""
                else:
                    poi_addr = str(addr_raw).strip()
                loc = p.get("location", "")
                if not loc or "," not in loc:
                    continue
                lon, lat = map(float, loc.split(","))

                if city_bounds and not coord_in_bounds(lat, lon, city_bounds):
                    continue
                if district_bounds and Polygon is not None and Point is not None:
                    if not district_bounds.contains(Point(lon, lat)):
                        continue
                if poi_name and not _names_match(name_for_match, poi_name):
                    if not _names_match_loose(name_for_match, poi_name):
                        continue
                if target_district:
                    actual = reverse_geocode_district(lat, lon, key, provider=provider)
                    time.sleep(0.15)
                    if not actual or not district_match(target_district, actual):
                        continue

                candidates.append({
                    "lon": lon, "lat": lat, "address": poi_addr,
                    "name": poi_name, "query": query,
                })
    except Exception:
        pass
    return candidates


def _try_poi_single_query_tencent(query, key, city, city_bounds, district_bounds,
                                  target_district, name_for_match):
    """Try one POI query via Tencent Map and return all valid candidates."""
    url = (
        f"https://apis.map.qq.com/ws/place/v1/search"
        f"?keyword={urllib.parse.quote(query)}"
        f"&boundary=region({urllib.parse.quote(city)},0)"
        f"&page_size=5&page_index=1"
        f"&key={key}"
    )
    candidates = []
    try:
        resp = requests.get(url, timeout=10).json()
        if resp.get("status") == 0 and resp.get("data"):
            for p in resp["data"]:
                poi_name = p.get("title", "")
                poi_addr = str(p.get("address", "")).strip()
                loc = p.get("location", {})
                if not loc or "lat" not in loc or "lng" not in loc:
                    continue
                lat, lon = loc["lat"], loc["lng"]

                if city_bounds and not coord_in_bounds(lat, lon, city_bounds):
                    continue
                if district_bounds and Polygon is not None and Point is not None:
                    if not district_bounds.contains(Point(lon, lat)):
                        continue
                if poi_name and not _names_match(name_for_match, poi_name):
                    if not _names_match_loose(name_for_match, poi_name):
                        continue
                if target_district:
                    actual = reverse_geocode_district(lat, lon, key, provider="tencent")
                    time.sleep(0.15)
                    if not actual or not district_match(target_district, actual):
                        continue

                candidates.append({
                    "lon": lon, "lat": lat, "address": poi_addr,
                    "name": poi_name, "query": query,
                })
    except Exception:
        pass
    return candidates


def poi_search(name, key, city="福州", district_filter=None, target_district=None,
               city_bounds=None, district_bounds=None, variant_queries=None,
               provider="gaode"):
    """Search POI by enterprise name with name-matching and district filtering.

    Supports both Gaode and Tencent map providers.

    Returns (lon, lat, address, level) or (None, None, None, None).
    """
    all_queries = [name]
    if variant_queries:
        all_queries.extend(variant_queries)

    all_candidates = []
    for q in all_queries:
        if provider == "tencent":
            candidates = _try_poi_single_query_tencent(
                q, key, city, city_bounds, district_bounds, target_district, name
            )
        else:
            candidates = _try_poi_single_query(
                q, key, city, city_bounds, district_bounds, target_district, name,
                provider=provider,
            )
        all_candidates.extend(candidates)

    if not all_candidates:
        return None, None, None, None

    if len(all_candidates) == 1:
        c = all_candidates[0]
        return c["lon"], c["lat"], c["address"], "POI"

    _PROD_KWS = ["厂", "基地", "园区", "工业区", "产业园", "处理厂", "电厂",
                 "电站", "院区", "校区", "实验中心", "科创园", "科技园",
                 "屠宰场", "养殖场", "牧场", "风电场"]

    def _score(c):
        score = 0
        poi_name = c["name"]
        query = c["query"]
        if query == name:
            score += 3
        else:
            score -= 2
        for kw in _PROD_KWS:
            if kw in poi_name:
                score += 10
                break
        return score

    best = max(all_candidates, key=_score)
    return best["lon"], best["lat"], best["address"], "POI"


def geocode_with_validation(name, address, key, city="福州", rate_limit=0.15,
                             target_district=None, enterprises=None,
                             provider="gaode", enterprise_district=None):
    """Dual-validation geocoding: name POI search + address geocoding with cross-check.

    Args:
        enterprises: Optional list of enterprise dicts for district bounds inference.
        provider: 'gaode' or 'tencent'. If empty, uses MAP_PROVIDER env var.
        enterprise_district: The district this specific enterprise belongs to
            (from source roster). Takes priority over target_district for
            district-level validation to avoid cross-district mismatches.

    Returns a dict:
      {
        "lat": float, "lon": float, "level": str,
        "method": "POI"|"address"|"POI+cross-check"|"failed",
        "warnings": [str, ...],
        "poi_addr": str,  # POI address if POI found
      }
    """
    from utils import get_map_provider
    provider = provider or get_map_provider()

    result = {"lat": None, "lon": None, "level": None, "method": "failed", "warnings": [], "poi_addr": ""}

    has_address = bool(address and address.strip())
    original_empty = not has_address

    # Use enterprise's own district for validation if available;
    # fallback to config-level target_district for bounds inference.
    effective_district = enterprise_district or target_district

    # Auto-infer bounds for this city/district
    city_bounds = estimate_city_bounds(city)
    district_bounds = None
    if target_district:
        district_bounds = get_district_bounds(city, target_district, enterprises)

    # If address is empty, infer one from name + city + district
    inferred_address = ""
    if not has_address and effective_district:
        inferred_address = infer_address_from_name(name, city, effective_district)
        if inferred_address:
            address = inferred_address
            has_address = True

    # Enrich generic addresses with target district to improve accuracy
    if has_address and effective_district:
        district_base = effective_district.replace("区", "").replace("县", "").replace("市", "")
        has_district = district_base in address or effective_district in address
        if not has_district:
            if address.startswith(city):
                address = address.replace(city, f"{city}{effective_district}", 1)
            else:
                address = f"{city}{effective_district}{address}"

    # Step 1: Name-based POI search (with district filtering + name matching + bounds)
    # Also try production-address variants (e.g. "XX厂区", "XX生产基地")
    # to avoid matching the headquarters office instead of the actual facility.
    variant_queries = build_production_queries(name, city, effective_district or "")
    poi_lon, poi_lat, poi_addr, poi_level = poi_search(
        name, key, city, target_district=effective_district,
        city_bounds=city_bounds, district_bounds=district_bounds,
        variant_queries=variant_queries,
        provider=provider,
    )
    time.sleep(rate_limit)

    if poi_lon is not None:
        result["lat"] = poi_lat
        result["lon"] = poi_lon
        result["level"] = poi_level
        result["method"] = "POI"
        result["poi_addr"] = poi_addr

    # If no address (and no inferred address), rely solely on POI
    if not has_address:
        if poi_lon is None:
            result["warnings"].append(
                "该企业缺少address字段，且POI搜索未找到匹配结果。"
                "建议通过企业官网、环评公示或天眼查补充实际经营地址后重新编码。"
            )
        else:
            result["warnings"].append(
                "该企业缺少address字段，坐标仅来自POI搜索，无地址交叉验证。"
                "建议补充实际地址以提高可靠性。"
            )
        return result

    # Step 2: Address geocoding (city-restricted to avoid cross-province matches)
    addr_lon, addr_lat, addr_level = geocode_address(address, key, city=city, provider=provider)
    time.sleep(rate_limit)

    if addr_lon is None:
        if poi_lon is None:
            result["warnings"].append("POI和地址编码均失败")
        else:
            result["warnings"].append("地址编码失败，采用POI坐标")
        return result

    # Validate address geocoding against city bounds
    if not coord_in_bounds(addr_lat, addr_lon, city_bounds):
        result["warnings"].append(
            f"地址编码结果({addr_lat:.5f},{addr_lon:.5f})超出城市范围，"
            f"可能是地址错误。"
        )
        if poi_lon is not None:
            # Revert to POI since address geocoding is clearly wrong
            result["warnings"][-1] += " 采用POI坐标替代。"
            result["method"] = "POI+cross-check"
            return result

    # Step 3: Cross-validation when both methods succeed
    if poi_lon is not None:
        dist = haversine(poi_lat, poi_lon, addr_lat, addr_lon)
        if dist > 5000:
            # Large deviation often means POI is the registered office while
            # the provided address is the actual facility.
            prefer_address = True
            if effective_district:
                addr_actual = reverse_geocode_district(addr_lat, addr_lon, key, provider=provider)
                time.sleep(rate_limit)
                if addr_actual and not district_match(effective_district, addr_actual):
                    prefer_address = False
                    result["warnings"].append(
                        f"POI与地址编码相距{dist:.0f}m，但地址编码结果位于{addr_actual}"
                        f"（非目标区），采用POI坐标"
                    )
                    result["method"] = "POI+cross-check"

            if prefer_address:
                result["lat"] = addr_lat
                result["lon"] = addr_lon
                result["level"] = addr_level
                result["warnings"].append(
                    f"POI与地址编码相距{dist:.0f}m，采用地址编码结果"
                    f"（POI可能为注册地址，地址编码指向实际设施）"
                )
                result["method"] = "address+cross-check"
        elif dist > 1000:
            result["warnings"].append(
                f"偏差{dist:.0f}m：POI({poi_lat:.5f},{poi_lon:.5f}) vs 地址({addr_lat:.5f},{addr_lon:.5f})"
            )
            result["method"] = "POI+cross-check"
        else:
            result["method"] = "POI+cross-check(OK)"
        return result

    # Step 4: POI not found, use address geocoding
    result["lat"] = addr_lat
    result["lon"] = addr_lon
    result["level"] = addr_level
    result["method"] = "address"
    if original_empty and inferred_address:
        result["method"] = "address(inferred)"

    # Step 5: Fuzzy address quality check
    fuzzy_levels = {"区县", "乡镇", "村庄", "未知", "兴趣点"}
    if addr_level in fuzzy_levels:
        result["warnings"].append(
            f"地址精度不足[{addr_level}]，坐标可能为区域中心点，建议补充到门牌号级别"
        )

    return result


def load_cache(cache_file: str) -> dict:
    if os.path.exists(cache_file):
        with open(cache_file, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_cache(cache_file: str, cache: dict):
    os.makedirs(os.path.dirname(cache_file) or ".", exist_ok=True)
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def run_geocode(config_path: str, force: bool = False):
    config = load_config_with_defaults(config_path)

    config_dir = os.path.dirname(os.path.abspath(config_path))
    cache_file = resolve_cache_path(
        config, config_dir, config["gaode"].get("cache_file", "./geocode_cache.json")
    )
    config["gaode"]["cache_file"] = cache_file

    provider = config["gaode"].get("provider", "") or get_map_provider()
    key = get_map_key(provider, config["gaode"]["key"])
    if not key:
        print(f"ERROR: No API key for provider '{provider}'. Set GAODE_API_KEY or TENCENT_MAP_KEY env var, or set gaode.key in config.")
        return
    rate_limit = config["gaode"].get("rate_limit", 0.15)
    city = config["gaode"].get("city", "福州")

    cache = {} if force else load_cache(cache_file)
    enterprises = config.get("enterprises", [])
    filename_hint = os.path.basename(config_path) if config_path else ""
    target_district = extract_target_district(config, filename_hint=filename_hint)

    need_geocode = [e for e in enterprises if "lat" not in e or "lon" not in e or force]

    print("=" * 60)
    print(f"Geocoding {len(need_geocode)} enterprises (POI search优先 + 地址编码交叉验证)")
    print(f"City: {city}")
    print("=" * 60)

    fixed_count = 0
    fuzzy_count = 0
    failed_count = 0
    seen_coords = {}  # Deduplicate by (name, address) within this run

    for i, e in enumerate(need_geocode, 1):
        name = e["name"]
        address = e["address"]

        dedup_key = (name, address)
        if dedup_key in seen_coords:
            cached = seen_coords[dedup_key]
            e["lat"] = cached["lat"]
            e["lon"] = cached["lon"]
            e["geocode_level"] = cached["geocode_level"]
            print(f"\n{i}. ○ {name} (复用同地址编码结果)")
            print(f"   坐标: {cached['lat']:.6f}, {cached['lon']:.6f} [{cached['geocode_level']}]")
            fixed_count += 1
            continue

        enterprise_district = e.get("district", "")
        result = geocode_with_validation(name, address, key, city=city, rate_limit=rate_limit, target_district=target_district, enterprises=enterprises, provider=provider, enterprise_district=enterprise_district)

        if result["lat"] is None:
            print(f"\n{i}. ✗ FAILED: {name}")
            failed_count += 1
            continue

        # Check if this is an improvement over cached data
        cache_key = f"{name}|{address}"
        old = cache.get(cache_key)
        improved = True
        if old and not force:
            old_dist = haversine(old["lat"], old["lon"], result["lat"], result["lon"])
            if old_dist < 100:
                improved = False

        needs_coords = "lat" not in e or "lon" not in e

        if improved or needs_coords:
            e["lat"] = result["lat"]
            e["lon"] = result["lon"]
            e["geocode_level"] = result["level"]
            cache[cache_key] = {"lat": result["lat"], "lon": result["lon"], "level": result["level"]}
            seen_coords[dedup_key] = {"lat": result["lat"], "lon": result["lon"], "geocode_level": result["level"]}
            fixed_count += 1

        print(f"\n{i}. {'✓' if improved else '○'} {name}")
        print(f"   坐标: {result['lat']:.6f}, {result['lon']:.6f} [{result['level']}] (来源: {result['method']})")
        if result["poi_addr"]:
            print(f"   POI地址: {result['poi_addr']}")
        for w in result["warnings"]:
            print(f"   ⚠ {w}")

        if result["level"] in {"区县", "乡镇", "村庄", "未知"}:
            fuzzy_count += 1

    save_cache(cache_file, cache)

    print("\n" + "=" * 60)
    print(f"结果: 成功/更新 {fixed_count}, 精度不足 {fuzzy_count}, 失败 {failed_count}")
    print(f"缓存: {cache_file}")
    print("=" * 60)

    # ── Cross-district validation (pre-condition) ──
    if key:
        print("\n" + "=" * 60)
        print(f"跨区校验: 目标区 = {target_district}")
        print("=" * 60)
        cross_district = []
        for e in enterprises:
            if "lat" not in e or "lon" not in e:
                continue
            actual = reverse_geocode_district(e["lat"], e["lon"], key, provider=provider)
            time.sleep(rate_limit)
            # Use enterprise's own district for validation if available;
            # fallback to config-level target_district.
            expected = e.get("district", "") or target_district
            if not expected:
                continue
            if actual and not district_match(expected, actual):
                cross_district.append({
                    "name": e["name"],
                    "address": e.get("address", ""),
                    "target": expected,
                    "actual": actual,
                    "lat": e["lat"],
                    "lon": e["lon"],
                })
                e["geocode_level"] = "跨区"

        if cross_district:
            print(f"\n🚨 发现 {len(cross_district)} 家企业坐标不在目标区：")
            for item in cross_district:
                print(f"   - {item['name']}")
                print(f"     地址: {item['address']}")
                print(f"     坐标: ({item['lat']:.5f}, {item['lon']:.5f})")
                print(f"     实际位于: {item['actual']}（目标: {item['target']}）")
            print("\n这些企业可能使用了注册地址/总部地址，而非目标区内的实际厂区地址。")
            print("请核实后更新地址，或将其从本区名单中移除。")
        else:
            print("\n✓ 全部企业坐标位于目标区内")
        print("=" * 60)

    # Save updated config
    write_yaml_config(config, config_path)
    print(f"配置已更新: {config_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Batch geocode with dual validation")
    parser.add_argument("-c", "--config", required=True, help="Path to config YAML file")
    parser.add_argument("--force", action="store_true", help="Force re-geocode (ignore cache)")
    parser.add_argument("--provider", default="", choices=["gaode", "tencent"],
                        help="Map provider (gaode or tencent). Overrides MAP_PROVIDER env var.")
    args = parser.parse_args()

    if args.provider:
        os.environ["MAP_PROVIDER"] = args.provider
    run_geocode(args.config, force=args.force)
