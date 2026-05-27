#!/usr/bin/env python3
"""Audit and fix enterprise coordinates by cross-checking with Gaode POI.

Usage:
    python audit_coords.py --report -c config.yaml    # print audit report only
    python audit_coords.py --fix -c config.yaml       # auto-fix deviations >500m
"""
import math
import os
import sys
import time
import urllib.parse

import requests
import yaml

sys.path.insert(0, os.path.dirname(__file__))
from utils import get_map_key, write_yaml_config, _names_match, extract_district_hint, district_match, reverse_geocode_district, load_config_with_defaults

KEY = get_map_key("")
RATE_LIMIT = 0.12


def haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def poi_search(name, city="福州"):
    """Search Gaode POI by name. Returns (lon, lat, address, poi_name) or (None, None, None, None)."""
    url = (
        f"https://restapi.amap.com/v3/place/text"
        f"?keywords={urllib.parse.quote(name)}"
        f"&city={urllib.parse.quote(city)}"
        f"&offset=1&page=1&key={KEY}&extensions=all"
    )
    try:
        resp = requests.get(url, timeout=10).json()
        if resp.get("status") == "1" and resp.get("pois"):
            p = resp["pois"][0]
            lon, lat = map(float, p["location"].split(","))
            return lon, lat, p.get("address", ""), p.get("name", "")
    except Exception:
        pass
    return None, None, None, None


def geocode_address(address, city="福州"):
    """Geocode an address and return (lat, lon, district) or (None, None, None)."""
    url = (
        f"https://restapi.amap.com/v3/geocode/geo"
        f"?address={urllib.parse.quote(address)}"
        f"&key={KEY}&city={urllib.parse.quote(city)}"
    )
    try:
        resp = requests.get(url, timeout=10).json()
        if resp.get("status") == "1" and resp.get("geocodes"):
            g = resp["geocodes"][0]
            lon, lat = map(float, g["location"].split(","))
            return lat, lon, g.get("district", "")
    except Exception:
        pass
    return None, None, None




def audit_config(path: str, fix: bool = False):
    config = load_config_with_defaults(path)

    enterprises = config["enterprises"]
    # Filter out enterprises without coordinates (geocode failed)
    enterprises_with_coords = [e for e in enterprises if "lat" in e and "lon" in e]
    skipped = [e["name"] for e in enterprises if "lat" not in e or "lon" not in e]
    total = len(enterprises_with_coords)

    # Stats
    ok_count = 0
    fixed_count = 0
    not_found = []
    deviations = []  # (name, dist, cur_lat, cur_lon, poi_lat, poi_lon, poi_addr, is_outlier, name_mismatch)
    fuzzy_count = 0
    out_of_bounds = []
    name_mismatches = []  # (name, poi_name)
    district_mismatches = []  # (name, name_hint, addr_district, address)
    coord_district_mismatches = []  # (name, expected_district, actual_district, poi_addr)

    # Centroid for outlier detection
    lats = [e["lat"] for e in enterprises_with_coords]
    lons = [e["lon"] for e in enterprises_with_coords]
    centroid_lat = sum(lats) / len(lats)
    centroid_lon = sum(lons) / len(lons)
    std_lat = (sum((x - centroid_lat) ** 2 for x in lats) / len(lats)) ** 0.5
    std_lon = (sum((x - centroid_lon) ** 2 for x in lons) / len(lons)) ** 0.5

    print("=" * 70)
    print(f"坐标审计: {path} ({total}家企业)")
    if skipped:
        print(f"⚠ 跳过 {len(skipped)} 家无坐标企业: {', '.join(skipped)}")
    print("=" * 70)

    for i, e in enumerate(enterprises_with_coords, 1):
        name = e["name"]
        cur_lat = e["lat"]
        cur_lon = e["lon"]
        level = e.get("geocode_level", "未知")

        # Check bounds
        if not (23.0 <= cur_lat <= 29.0 and 115.0 <= cur_lon <= 121.0):
            out_of_bounds.append((name, cur_lat, cur_lon))

        # Reverse geocode: verify which district the coordinate actually falls in
        actual_district = reverse_geocode_district(cur_lat, cur_lon, KEY)
        time.sleep(RATE_LIMIT)
        name_hint = extract_district_hint(name)
        if name_hint and actual_district and not district_match(name_hint, actual_district):
            coord_district_mismatches.append((name, name_hint, actual_district, ""))

        # Check precision
        if level in {"区县", "乡镇", "村庄", "未知"}:
            fuzzy_count += 1

        # Address-district consistency check
        addr_lat, addr_lon, addr_district = geocode_address(e["address"])
        time.sleep(RATE_LIMIT)
        name_hint = extract_district_hint(name)
        if name_hint and addr_district and not district_match(name_hint, addr_district):
            district_mismatches.append((name, name_hint, addr_district, e.get("address", "")))

        # POI search
        poi_lon, poi_lat, poi_addr, poi_name = poi_search(name)
        time.sleep(RATE_LIMIT)

        if poi_lon is None:
            not_found.append(name)
            continue

        # Name mismatch check: POI name should match enterprise name
        name_mismatch = False
        if poi_name and not _names_match(name, poi_name):
            name_mismatches.append((name, poi_name))
            name_mismatch = True

        dist = haversine(cur_lat, cur_lon, poi_lat, poi_lon)

        # Skip deviation check if actual address has been manually verified
        verified_actual = e.get("actual_address_verified", False)

        # Outlier detection
        d_lat = abs(cur_lat - centroid_lat)
        d_lon = abs(cur_lon - centroid_lon)
        is_outlier = (d_lat > 3 * std_lat + 0.001 or d_lon > 3 * std_lon + 0.001)
        if is_outlier:
            dist_to_centroid = ((d_lat * 111000) ** 2 + (d_lon * 111000 * math.cos(math.radians(centroid_lat))) ** 2) ** 0.5
            is_outlier = dist_to_centroid > 15000

        if (dist <= 500 and not is_outlier) or verified_actual:
            ok_count += 1
        else:
            deviations.append((name, dist, cur_lat, cur_lon, poi_lat, poi_lon, poi_addr, is_outlier, name_mismatch))
            if fix and not name_mismatch and not verified_actual:
                e["lat"] = poi_lat
                e["lon"] = poi_lon
                if poi_addr:
                    old_addr = e.get("address", "")
                    if len(poi_addr) > len(old_addr) * 0.5:
                        e["address"] = f"福州市{poi_addr}"
                        lines = e["label"].split("\n")
                        if len(lines) >= 2:
                            lines[-1] = poi_addr
                            e["label"] = "\n".join(lines)
                fixed_count += 1

    # Print report
    print(f"\n{'✓' if ok_count == total else '○'} 坐标准确: {ok_count}/{total}")

    if deviations:
        print(f"\n⚠️  坐标偏差 >500m 或异常偏离 ({len(deviations)}家):")
        print("-" * 70)
        for name, dist, cur_lat, cur_lon, poi_lat, poi_lon, poi_addr, is_outlier, name_mismatch in deviations:
            marker = "🚨" if is_outlier else "⚠️"
            mismatch_tag = " [POI名称不匹配!]" if name_mismatch else ""
            print(f"{marker} {name}{mismatch_tag}")
            print(f"   偏差: {dist:.0f}m{' (异常偏离!)' if is_outlier else ''}")
            print(f"   当前: {cur_lat:.5f},{cur_lon:.5f}")
            print(f"   POI:  {poi_lat:.5f},{poi_lon:.5f}  {poi_addr}")
            if name_mismatch:
                print(f"   ⚠ POI名称不匹配，自动修正已跳过，请人工核实")

    if not_found:
        print(f"\n❌ POI未找到 ({len(not_found)}家):")
        for n in not_found:
            print(f"   - {n}")

    if name_mismatches:
        print(f"\n🚫 POI名称不匹配 ({len(name_mismatches)}家):")
        for name, poi_name in name_mismatches:
            print(f"   - {name}: POI返回'{poi_name}'")
        print("   这些企业的POI搜索结果可能为同名/近似名干扰，坐标未被自动修正。")

    if out_of_bounds:
        print(f"\n🚨 超出福建省范围 ({len(out_of_bounds)}家):")
        for name, lat, lon in out_of_bounds:
            print(f"   - {name}: ({lat:.5f}, {lon:.5f})")

    if district_mismatches:
        print(f"\n🏢 注册地址与行政区不符 ({len(district_mismatches)}家):")
        for name, hint, addr_district, address in district_mismatches:
            print(f"   - {name}")
            print(f"     名称含'{hint}'，但地址('{address}')编码到'{addr_district}'")
        print("     建议：这些企业可能使用了注册地址而非实际污染源地址，")
        print("     请通过企业官网/环评公示/政府公告核实实际经营地址。")

    if coord_district_mismatches:
        print(f"\n🗺️  坐标行政区不符 ({len(coord_district_mismatches)}家):")
        for name, expected, actual, _ in coord_district_mismatches:
            print(f"   - {name}")
            print(f"     坐标实际位于 '{actual}'，但企业名称含 '{expected}'")
        print("     建议：该POI可能是同名企业（跨区/跨市）或注册地址，")
        print("     请核实实际污染源地址，确认后标记 actual_address_verified: true")

    if fuzzy_count:
        print(f"\n📍 精度不足（乡镇/村庄级）({fuzzy_count}家)")

    print("\n" + "=" * 70)
    if fix:
        write_yaml_config(config, path)
        print(f"已自动修正 {fixed_count} 家企业，保存至: {path}")
    else:
        print("审计完成。如需自动修正，添加 --fix 参数")
    print("=" * 70)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Audit enterprise coordinates against Gaode POI")
    parser.add_argument("-c", "--config", required=True, help="Path to config YAML file")
    parser.add_argument("--report", action="store_true", default=True, help="Print audit report only (default)")
    parser.add_argument("--fix", action="store_true", help="Auto-fix deviations >500m")
    args = parser.parse_args()

    if not KEY:
        print("ERROR: No Gaode API key. Set GAODE_API_KEY env var.")
        sys.exit(1)

    audit_config(args.config, fix=args.fix)
