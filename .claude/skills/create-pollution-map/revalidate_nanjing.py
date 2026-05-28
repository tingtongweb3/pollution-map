#!/usr/bin/env python3
"""
Re-geocode all Nanjing enterprises with per-enterprise district validation
and generate a correction report comparing old vs new coordinates.
"""

import json
import math
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from geocode import geocode_with_validation, haversine
from utils import get_map_key, get_map_provider, reverse_geocode_district, district_match


def load_dotenv():
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    os.environ[k] = v


def main():
    load_dotenv()

    provider = get_map_provider()
    key = get_map_key(provider, "")
    city = "南京"
    rate_limit = 0.15

    cache_path = Path("/Users/wong/.hermes/code/map2image/data/南京/geocode_cache_南京.json")
    data_path = Path("/Users/wong/.hermes/code/map2image/data/南京/南京_2025_city_cache.json")

    with open(cache_path, "r", encoding="utf-8") as f:
        old_cache = json.load(f)

    with open(data_path, "r", encoding="utf-8") as f:
        enterprises = json.load(f)

    print(f"Provider: {provider}")
    print(f"Total enterprises: {len(enterprises)}")
    print(f"Old cache entries: {len(old_cache)}")
    print("=" * 60)

    corrections = []
    new_failures = []
    unchanged = []
    skipped_no_old = []

    for i, e in enumerate(enterprises, 1):
        name = e["name"]
        address = e.get("address", "")
        district = e.get("district", "")
        old_key = f"{name}|{address}"
        old = old_cache.get(old_key)

        print(f"\n[{i}/{len(enterprises)}] {name}")
        if district:
            print(f"    归属区: {district}")

        result = geocode_with_validation(
            name, address, key,
            city=city, rate_limit=rate_limit,
            target_district=district,  # use enterprise's own district as primary
            enterprises=enterprises,
            provider=provider,
            enterprise_district=district,
        )

        new_lat = result["lat"]
        new_lon = result["lon"]
        method = result["method"]
        warnings = result["warnings"]

        if warnings:
            for w in warnings:
                print(f"    ⚠ {w}")

        if old and old.get("lat") is not None and old.get("lon") is not None:
            old_lat, old_lon = old["lat"], old["lon"]

            if new_lat is None or new_lon is None:
                # Previously had coordinates, now failed
                new_failures.append({
                    "name": name,
                    "district": district,
                    "old": (old_lat, old_lon),
                    "new": None,
                    "reason": "新逻辑下POI/地址编码均失败或被district过滤",
                    "method": method,
                    "warnings": warnings,
                })
                print(f"    ✗ 旧坐标丢失: ({old_lat:.5f}, {old_lon:.5f}) → FAILED")
            elif abs(old_lat - new_lat) > 0.0001 or abs(old_lon - new_lon) > 0.0001:
                dist = haversine(old_lat, old_lon, new_lat, new_lon)
                corrections.append({
                    "name": name,
                    "district": district,
                    "old": (old_lat, old_lon),
                    "new": (new_lat, new_lon),
                    "distance_m": round(dist),
                    "method": method,
                    "warnings": warnings,
                })
                print(f"    ✓ 坐标修正: ({old_lat:.5f}, {old_lon:.5f}) → ({new_lat:.5f}, {new_lon:.5f}) 偏差{dist:.0f}m")
            else:
                unchanged.append(name)
                print(f"    ○ 坐标不变: ({new_lat:.5f}, {new_lon:.5f})")
        else:
            if new_lat is not None and new_lon is not None:
                skipped_no_old.append({
                    "name": name,
                    "district": district,
                    "new": (new_lat, new_lon),
                    "method": method,
                })
                print(f"    ✓ 新获取: ({new_lat:.5f}, {new_lon:.5f})")
            else:
                new_failures.append({
                    "name": name,
                    "district": district,
                    "old": None,
                    "new": None,
                    "reason": "旧无坐标，新逻辑仍失败",
                    "method": method,
                    "warnings": warnings,
                })
                print(f"    ✗ 仍失败")

    # ---- Report ----
    print("\n" + "=" * 60)
    print("修正报告")
    print("=" * 60)
    print(f"总企业数: {len(enterprises)}")
    print(f"坐标被修正: {len(corrections)}")
    print(f"旧坐标丢失(新逻辑失败): {len([f for f in new_failures if f['old'] is not None])}")
    print(f"仍无坐标: {len([f for f in new_failures if f['old'] is None])}")
    print(f"新获取坐标(旧无): {len(skipped_no_old)}")
    print(f"坐标不变: {len(unchanged)}")
    print("=" * 60)

    if corrections:
        print("\n【坐标被修正的企业】")
        for c in corrections:
            print(f"\n  {c['name']} (归属: {c['district']})")
            print(f"    旧: ({c['old'][0]:.6f}, {c['old'][1]:.6f})")
            print(f"    新: ({c['new'][0]:.6f}, {c['new'][1]:.6f})")
            print(f"    偏差: {c['distance_m']}m")
            print(f"    方法: {c['method']}")
            if c['warnings']:
                for w in c['warnings']:
                    print(f"    ⚠ {w}")

    # Cross-district check on NEW coordinates
    print("\n" + "=" * 60)
    print("新坐标跨区校验")
    print("=" * 60)
    cross_district = []
    for c in corrections:
        lat, lon = c["new"]
        actual = reverse_geocode_district(lat, lon, key, provider=provider)
        time.sleep(rate_limit)
        if actual and c["district"] and not district_match(c["district"], actual):
            cross_district.append({
                "name": c["name"],
                "district": c["district"],
                "actual": actual,
                "lat": lat,
                "lon": lon,
            })

    for item in skipped_no_old:
        lat, lon = item["new"]
        actual = reverse_geocode_district(lat, lon, key, provider=provider)
        time.sleep(rate_limit)
        if actual and item["district"] and not district_match(item["district"], actual):
            cross_district.append({
                "name": item["name"],
                "district": item["district"],
                "actual": actual,
                "lat": lat,
                "lon": lon,
            })

    if cross_district:
        print(f"\n🚨 发现 {len(cross_district)} 家修正后坐标仍跨区：")
        for item in cross_district:
            print(f"   - {item['name']}")
            print(f"     归属: {item['district']}, 实际: {item['actual']}")
            print(f"     坐标: ({item['lat']:.5f}, {item['lon']:.5f})")
    else:
        print("\n✓ 全部修正后坐标位于归属区内")

    # Save report
    report = {
        "summary": {
            "total": len(enterprises),
            "corrected": len(corrections),
            "lost": len([f for f in new_failures if f["old"] is not None]),
            "still_failed": len([f for f in new_failures if f["old"] is None]),
            "newly_acquired": len(skipped_no_old),
            "unchanged": len(unchanged),
            "cross_district_after_fix": len(cross_district),
        },
        "corrections": corrections,
        "new_failures": new_failures,
        "skipped_no_old": skipped_no_old,
        "cross_district": cross_district,
    }
    report_path = Path("/Users/wong/.hermes/code/map2image/data/南京/revalidation_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n报告已保存: {report_path}")


if __name__ == "__main__":
    main()
