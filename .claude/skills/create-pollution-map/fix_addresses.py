#!/usr/bin/env python3
"""
Fix pseudo addresses by searching Gaode POI with strict name matching.

This script addresses the root cause of coordinate errors: when the address
field is just a placeholder like "厦门市集美区厦门成联五金制造有限公司",
the geocoder has no real street/number to work with, so it falls back to
POI name search which may match the wrong company (same name in wrong district,
or a similar-named company).

Workflow:
  1. Identify enterprises with pseudo addresses (no street/number/door info)
  2. For each, search Gaode POI by company name with STRICT matching
  3. If POI found and name matches exactly → update address to POI address
  4. If POI not found or name mismatched → flag for manual web search

Usage:
    python fix_addresses.py -c config.yaml           # dry-run report
    python fix_addresses.py -c config.yaml --apply   # update YAML

After running this, run geocode.py to re-geocode with the improved addresses.
"""

import argparse
import os
import sys
import time
import urllib.parse

import requests
import yaml

sys.path.insert(0, os.path.dirname(__file__))
from utils import (
    get_gaode_key,
    is_pseudo_address,
    address_quality_score,
    _names_match,
    extract_district_hint,
    district_match,
    write_yaml_config,
)


def _poi_search(name, key, city, district_filter=None):
    """Search Gaode POI by name with strict name + district filtering.

    Returns (poi_name, poi_addr, lon, lat, reason) or (None, None, None, None, reason).
    """
    url = (
        f"https://restapi.amap.com/v3/place/text"
        f"?keywords={urllib.parse.quote(name)}"
        f"&city={urllib.parse.quote(city)}"
        f"&offset=1&page=1&key={key}&extensions=all"
    )
    try:
        resp = requests.get(url, timeout=10).json()
        if resp.get("status") != "1" or not resp.get("pois"):
            return None, None, None, None, "高德POI无结果"

        p = resp["pois"][0]
        poi_name = p.get("name", "")
        poi_addr = p.get("address", "")
        loc = p.get("location", "")
        if not loc or "," not in loc:
            return None, None, None, None, "POI坐标缺失"

        lon, lat = map(float, loc.split(","))

        # Strict name matching
        if poi_name and not _names_match(name, poi_name):
            return None, None, None, None, f"名称不匹配: POI='{poi_name}'"

        # District filter: POI address must contain the district keyword
        if district_filter and poi_addr and district_filter not in poi_addr:
            return None, None, None, None, f"行政区不匹配: POI地址在'{poi_addr}'"

        return poi_name, poi_addr, lon, lat, "OK"

    except Exception as e:
        return None, None, None, None, f"搜索异常: {e}"


def _reverse_check_district(lat, lon, key, expected_district):
    """Reverse geocode to verify coordinate falls in expected district."""
    from utils import reverse_geocode_district
    try:
        actual = reverse_geocode_district(lat, lon, key)
        if actual and expected_district and not district_match(expected_district, actual):
            return False, actual
        return True, actual
    except Exception:
        return True, ""  # Be lenient if reverse geocode fails


def fix_addresses(config_path, apply=False, rate_limit=0.15):
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    key = get_gaode_key(config["gaode"].get("key", ""))
    if not key:
        print("ERROR: No Gaode API key. Set GAODE_API_KEY env var.")
        return

    city = config["gaode"].get("city", "福州")
    enterprises = config.get("enterprises", [])

    fixed = []          # Address updated from POI
    already_good = []   # Address already has street/number
    failed = []         # POI not found or mismatch — needs manual search

    print("=" * 70)
    print(f"地址修复: {config_path}")
    print(f"城市: {city} | 企业数: {len(enterprises)}")
    print("=" * 70)

    for i, e in enumerate(enterprises, 1):
        name = e["name"]
        address = e.get("address", "")
        quality = address_quality_score(address, name)

        if not quality["is_pseudo"]:
            already_good.append({
                "name": name,
                "address": address,
                "quality": quality,
            })
            continue

        # Try POI search
        district_hint = extract_district_hint(name)
        poi_name, poi_addr, lon, lat, reason = _poi_search(
            name, key, city, district_filter=district_hint
        )
        time.sleep(rate_limit)

        if poi_addr:
            # Extra safety: reverse geocode to verify district
            district_ok, actual_district = _reverse_check_district(
                lat, lon, key, district_hint
            )
            time.sleep(rate_limit)

            if not district_ok:
                failed.append({
                    "name": name,
                    "old_address": address,
                    "reason": f"POI坐标位于{actual_district}，不在目标{district_hint}区",
                    "poi_name": poi_name,
                    "poi_addr": poi_addr,
                    "quality": quality,
                })
                continue

            # Build full address with city prefix if missing
            new_addr = poi_addr
            if city not in new_addr:
                new_addr = f"{city}{new_addr}"

            if apply:
                e["address"] = new_addr
                # Clear any stale coordinates so geocode.py will re-geocode
                for k in ["lat", "lon", "geocode_level"]:
                    e.pop(k, None)

            fixed.append({
                "name": name,
                "old_address": address,
                "new_address": new_addr,
                "poi_name": poi_name,
                "quality": quality,
            })
        else:
            failed.append({
                "name": name,
                "old_address": address,
                "reason": reason,
                "quality": quality,
            })

    # ── Report ──
    print(f"\n{'─' * 70}")
    print("地址质量分布")
    print("─" * 70)
    level_counts = {}
    for e in enterprises:
        q = address_quality_score(e.get("address", ""), e["name"])
        level_counts[q["level"]] = level_counts.get(q["level"], 0) + 1
    for lvl in ["excellent", "good", "fair", "poor", "pseudo"]:
        if lvl in level_counts:
            print(f"  {lvl}: {level_counts[lvl]} 家")

    if fixed:
        print(f"\n{'─' * 70}")
        print(f"✓ 自动修复成功: {len(fixed)} 家 (POI搜索+严格匹配)")
        print("─" * 70)
        for item in fixed:
            print(f"\n  【{item['name']}】")
            print(f"    旧地址: {item['old_address']}")
            print(f"    新地址: {item['new_address']}")
            if item.get("poi_name") and item["poi_name"] != item["name"]:
                print(f"    POI名称: {item['poi_name']}")

    if already_good:
        print(f"\n{'─' * 70}")
        print(f"○ 已有真实地址: {len(already_good)} 家 (无需修复)")
        print("─" * 70)
        for item in already_good[:5]:
            print(f"  - {item['name']}: {item['address']}")
        if len(already_good) > 5:
            print(f"    ... 共 {len(already_good)} 家")

    if failed:
        print(f"\n{'─' * 70}")
        print(f"✗ 无法自动修复: {len(failed)} 家 (需网络检索或人工核实)")
        print("─" * 70)
        for item in failed:
            print(f"\n  【{item['name']}】")
            print(f"    当前地址: {item['old_address']}")
            print(f"    失败原因: {item['reason']}")

    print(f"\n{'=' * 70}")
    if apply:
        if fixed:
            write_yaml_config(config, config_path)
            print(f"已更新配置: {config_path}")
            print(f"\n下一步: 运行 geocode.py -c {config_path} 重新编码坐标")
        else:
            print("无修复项，配置未变更")
    else:
        print("本次为干跑模式，未修改文件。添加 --apply 执行修复。")
    print("=" * 70)

    return {
        "fixed": fixed,
        "already_good": already_good,
        "failed": failed,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fix pseudo addresses via Gaode POI")
    parser.add_argument("-c", "--config", required=True, help="Path to config YAML")
    parser.add_argument("--apply", action="store_true", help="Apply fixes to YAML")
    parser.add_argument("--rate-limit", type=float, default=0.15, help="API rate limit in seconds")
    args = parser.parse_args()

    fix_addresses(args.config, apply=args.apply, rate_limit=args.rate_limit)
