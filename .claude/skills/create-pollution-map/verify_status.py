#!/usr/bin/env python3
"""
Verify enterprise operating status via Gaode POI search API.

Usage:
    python verify_status.py -c config.yaml
"""

import argparse
import os
import sys
import time
import urllib.parse

import requests
import yaml

sys.path.insert(0, os.path.dirname(__file__))
from utils import get_gaode_key, load_config_with_defaults


def search_poi(name: str, city: str, key: str, timeout: int = 10) -> dict:
    """Search for a POI by name + city via Gaode API."""
    keywords = urllib.parse.quote(name)
    city_encoded = urllib.parse.quote(city)
    url = (
        f"https://restapi.amap.com/v3/place/text"
        f"?key={key}&keywords={keywords}&city={city_encoded}"
        f"&offset=1&page=1"
    )
    try:
        resp = requests.get(url, timeout=timeout).json()
        if resp.get("status") == "1" and resp.get("pois"):
            poi = resp["pois"][0]
            return {
                "found": True,
                "name": poi.get("name", ""),
                "address": poi.get("address", ""),
                "type": poi.get("type", ""),
                "location": poi.get("location", ""),
            }
    except Exception:
        pass
    return {"found": False}


def run_verify(config_path: str):
    config = load_config_with_defaults(config_path)

    key = get_gaode_key(config["gaode"]["key"])
    if not key:
        print("ERROR: No Gaode API key. Set GAODE_API_KEY env var or set gaode.key in config.")
        return
    city = config["meta"]["title"].split("市")[0].replace("省", "").split("区")[0]
    # Heuristic: extract city from title, e.g. "福州市仓山区..." -> "福州"
    # Better: let user specify city in config

    # Try to extract city from boundary or first enterprise address
    enterprises = config.get("enterprises", [])
    if enterprises:
        first_addr = enterprises[0].get("address", "")
        # Extract city from address like "福州市仓山区..."
        if "市" in first_addr:
            city = first_addr[: first_addr.index("市")]

    print(f"Using city for POI search: {city}\n")
    print(f"{'Status':<8} {'Name':<40} {'POI Name / Reason'}")
    print("-" * 90)

    results = []
    for e in enterprises:
        name = e["name"]
        addr = e.get("address", "")
        existing_status = e.get("status", "").strip()

        # Skip if already manually verified
        if existing_status:
            status = existing_status
            reason = f"preset: {status}"
        else:
            poi = search_poi(name, city, key)
            if poi["found"]:
                status = "在营"
                reason = poi["name"]
            else:
                # Fallback: search by address only
                addr_poi = search_poi(addr, city, key)
                if addr_poi["found"]:
                    status = "待核实"
                    reason = f"地址匹配但无企业名: {addr_poi['name']}"
                else:
                    status = "未找到"
                    reason = "高德POI无记录"
            time.sleep(0.2)

        results.append({"name": name, "status": status, "reason": reason})
        print(f"{status:<8} {name:<40} {reason}")

    # Summary
    status_counts = {}
    for r in results:
        status_counts[r["status"]] = status_counts.get(r["status"], 0) + 1

    print("-" * 90)
    print("\n状态统计:")
    for s, c in sorted(status_counts.items(), key=lambda x: -x[1]):
        print(f"  {s}: {c}家")

    # Output YAML snippet with status filled
    print("\n--- YAML snippet with status (copy into config) ---")
    for r in results:
        print(f"  - name: \"{r['name']}\"")
        print(f"    status: \"{r['status']}\"")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Verify enterprise status via Gaode POI")
    parser.add_argument("-c", "--config", required=True, help="Path to config YAML file")
    args = parser.parse_args()
    run_verify(args.config)
