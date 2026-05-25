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

sys.path.insert(0, os.path.dirname(__file__))
from utils import (
    geocode_address, get_gaode_key, write_yaml_config, _names_match,
    extract_district_hint, extract_target_district, district_match,
    reverse_geocode_district, resolve_cache_path, ensure_dir,
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


def poi_search(name, key, city="福州", district_filter=None, target_district=None):
    """Search Gaode POI by enterprise name with name-matching and district filtering.

    Args:
        district_filter: Obsolete, kept for compatibility.
        target_district: If given, use reverse geocoding to verify the coordinate
                         falls in this district (e.g. "丰泽区").

    Returns (lon, lat, address, level) or (None, None, None, None).
    """
    url = (
        f"https://restapi.amap.com/v3/place/text"
        f"?keywords={urllib.parse.quote(name)}"
        f"&city={urllib.parse.quote(city)}"
        f"&offset=3&page=1&key={key}&extensions=all"
    )
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

                # Name mismatch check (strict first, then loose)
                if poi_name and not _names_match(name, poi_name):
                    if not _names_match_loose(name, poi_name):
                        continue

                # District validation via reverse geocoding
                if target_district:
                    actual = reverse_geocode_district(lat, lon, key)
                    time.sleep(0.15)
                    if not actual:
                        continue  # Cannot verify district — unsafe to accept
                    if not district_match(target_district, actual):
                        continue

                return lon, lat, poi_addr, "POI"
    except Exception:
        pass
    return None, None, None, None


def geocode_with_validation(name, address, key, city="福州", rate_limit=0.15, target_district=None):
    """Dual-validation geocoding: name POI search + address geocoding with cross-check.

    Returns a dict:
      {
        "lat": float, "lon": float, "level": str,
        "method": "POI"|"address"|"POI+cross-check"|"failed",
        "warnings": [str, ...],
        "poi_addr": str,  # POI address if POI found
      }
    """
    result = {"lat": None, "lon": None, "level": None, "method": "failed", "warnings": [], "poi_addr": ""}

    has_address = bool(address and address.strip())

    # Enrich generic addresses with target district to improve accuracy
    if has_address and target_district:
        district_base = target_district.replace("区", "").replace("县", "").replace("市", "")
        all_districts = ["丰泽", "鲤城", "洛江", "泉港", "晋江", "石狮", "南安",
                         "惠安", "安溪", "永春", "德化", "仓山", "鼓楼", "台江",
                         "晋安", "马尾", "闽侯", "长乐", "福清", "连江", "罗源",
                         "闽清", "永泰"]
        has_district = any(d in address for d in all_districts + [target_district, district_base])
        if not has_district:
            if address.startswith("泉州"):
                address = address.replace("泉州", f"泉州市{target_district}", 1)
            else:
                address = f"泉州市{target_district}{address}"

    # Step 1: Name-based POI search (with district filtering + name matching)
    poi_lon, poi_lat, poi_addr, poi_level = poi_search(name, key, city, target_district=target_district)
    time.sleep(rate_limit)

    if poi_lon is not None:
        result["lat"] = poi_lat
        result["lon"] = poi_lon
        result["level"] = poi_level
        result["method"] = "POI"
        result["poi_addr"] = poi_addr

    # If no address, we cannot cross-validate. Rely solely on POI (which already passed name+district checks).
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
    addr_lon, addr_lat, addr_level = geocode_address(address, key, city=city)
    time.sleep(rate_limit)

    if addr_lon is None:
        if poi_lon is None:
            result["warnings"].append("POI和地址编码均失败")
        else:
            result["warnings"].append("地址编码失败，采用POI坐标")
        return result

    # Step 3: Cross-validation when both methods succeed
    if poi_lon is not None:
        dist = haversine(poi_lat, poi_lon, addr_lat, addr_lon)
        if dist > 5000:
            # Large deviation often means POI is the registered office while
            # the provided address is the actual facility. For pollution-source
            # maps, the actual facility location matters more.
            # BUT if the address geocoded outside the target district, the POI
            # is more likely correct (address string may be too generic).
            prefer_address = True
            if target_district:
                addr_actual = reverse_geocode_district(addr_lat, addr_lon, key)
                time.sleep(rate_limit)
                if addr_actual and not district_match(target_district, addr_actual):
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
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    config_dir = os.path.dirname(os.path.abspath(config_path))
    cache_file = resolve_cache_path(
        config, config_dir, config["gaode"].get("cache_file", "./geocode_cache.json")
    )
    config["gaode"]["cache_file"] = cache_file

    key = get_gaode_key(config["gaode"]["key"])
    if not key:
        print("ERROR: No Gaode API key. Set GAODE_API_KEY env var or set gaode.key in config.")
        return
    rate_limit = config["gaode"].get("rate_limit", 0.15)
    city = config["gaode"].get("city", "福州")

    cache = {} if force else load_cache(cache_file)
    enterprises = config.get("enterprises", [])
    target_district = extract_target_district(config)

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

        result = geocode_with_validation(name, address, key, city=city, rate_limit=rate_limit, target_district=target_district)

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
    if target_district and key:
        print("\n" + "=" * 60)
        print(f"跨区校验: 目标区 = {target_district}")
        print("=" * 60)
        cross_district = []
        for e in enterprises:
            if "lat" not in e or "lon" not in e:
                continue
            actual = reverse_geocode_district(e["lat"], e["lon"], key)
            time.sleep(rate_limit)
            if actual and not district_match(target_district, actual):
                cross_district.append({
                    "name": e["name"],
                    "address": e.get("address", ""),
                    "target": target_district,
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
    args = parser.parse_args()

    run_geocode(args.config, force=args.force)
