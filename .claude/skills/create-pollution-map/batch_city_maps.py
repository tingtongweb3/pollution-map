#!/usr/bin/env python3
"""
Batch generate pollution-source distribution maps for all districts of a city.

Given a city-level enterprise cache, this script:
  1. Partitions districts (auto-merge small ones if requested)
  2. Generates YAML configs per partition
  3. Runs geocode.py (shared cache)
  4. Runs create_map.py
  5. Prints a summary report

Usage:
    python batch_city_maps.py --city 南京 --year 2025 \
        --cache data/南京/南京_2025_city_cache.json \
        [--auto-merge] [--key GAODE_API_KEY]
"""

import argparse
import json
import os
import subprocess
import sys

import yaml

# Add skill directory to path
SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SKILL_DIR)

from collect_data import auto_partition_districts, generate_config, Enterprise
from utils import get_map_key, ensure_dir, get_map_provider


def _dict_to_enterprise(d: dict) -> Enterprise:
    """Convert a plain dict (from city cache JSON) to Enterprise dataclass."""
    # Filter keys that exist in the dataclass to avoid TypeError
    valid_keys = set(Enterprise.__dataclass_fields__.keys())
    filtered = {k: v for k, v in d.items() if k in valid_keys}
    return Enterprise(**filtered)


def load_city_cache(cache_path: str) -> list:
    """Load city cache and return list of enterprise dicts."""
    with open(cache_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"City cache must be a JSON list, got {type(data).__name__}")
    return data


def run_geocode_for_config(config_path: str, key: str = "", provider: str = ""):
    """Run geocode.py for a single config."""
    script = os.path.join(SKILL_DIR, "geocode.py")
    env = os.environ.copy()
    provider = provider or get_map_provider()
    if key:
        if provider == "tencent":
            env["TENCENT_MAP_KEY"] = key
        else:
            env["GAODE_API_KEY"] = key
    if provider:
        env["MAP_PROVIDER"] = provider
    result = subprocess.run(
        [sys.executable, script, "-c", config_path, "--provider", provider],
        env=env,
        capture_output=True,
        text=True,
    )
    return result


def run_create_map_for_config(config_path: str):
    """Run create_map.py for a single config."""
    script = os.path.join(SKILL_DIR, "create_map.py")
    result = subprocess.run(
        [sys.executable, script, "-c", config_path],
        capture_output=True,
        text=True,
    )
    return result


def _apply_alias(partition_name: str, aliases: dict) -> str:
    """If partition_name matches an alias key, return the alias value."""
    # Exact match
    if partition_name in aliases:
        return aliases[partition_name]
    # Try matching by parts
    for key, val in aliases.items():
        if set(key.split("+")) == set(partition_name.split("+")):
            return val
    return partition_name


def main():
    parser = argparse.ArgumentParser(
        description="Batch generate pollution-source maps for all city districts"
    )
    parser.add_argument("--city", required=True, help="City name (e.g. 南京)")
    parser.add_argument("--year", type=int, required=True, help="Year (e.g. 2025)")
    parser.add_argument("--cache", required=True, help="Path to city cache JSON")
    parser.add_argument("--output-dir", default="data", help="Output directory")
    parser.add_argument("--auto-merge", action="store_true",
                        help="Auto-merge small districts (<min-standalone) into groups")
    parser.add_argument("--min-standalone", type=int, default=20,
                        help="Minimum enterprises for a standalone district map")
    parser.add_argument("--max-merged", type=int, default=40,
                        help="Maximum enterprises per merged group")
    parser.add_argument("--alias", action="append",
                        help="Alias for merged partition (format: 主城区=玄武区+秦淮区+鼓楼区)")
    parser.add_argument("--key", default="",
                        help="API key (or set GAODE_API_KEY / TENCENT_MAP_KEY env var)")
    parser.add_argument("--provider", default="", choices=["gaode", "tencent"],
                        help="Map provider (gaode or tencent). Overrides MAP_PROVIDER env var.")
    parser.add_argument("--skip-geocode", action="store_true",
                        help="Skip geocoding step (use existing cache)")
    parser.add_argument("--skip-map", action="store_true",
                        help="Skip map generation step (only generate configs)")
    args = parser.parse_args()

    # Parse aliases
    aliases = {}
    if args.alias:
        for a in args.alias:
            if "=" in a:
                alias_name, parts_str = a.split("=", 1)
                aliases[parts_str.strip()] = alias_name.strip()

    # Load cache
    print(f"Loading city cache: {args.cache}")
    raw_ents = load_city_cache(args.cache)
    print(f"  Total enterprises: {len(raw_ents)}")

    # Convert to Enterprise objects
    enterprises = [_dict_to_enterprise(d) for d in raw_ents]

    # Check for missing district info
    has_district = any(e.district for e in enterprises)
    if not has_district:
        print("\nWARNING: City cache has no 'district' field on enterprises.")
        print("  Auto-merge will put all enterprises into a single partition.")
        print("  To fix, ensure the extraction step populates the 'district' field.")

    # Partition
    if args.auto_merge:
        partitions = auto_partition_districts(
            enterprises, args.min_standalone, args.max_merged
        )
    else:
        from collections import defaultdict
        district_ents = defaultdict(list)
        for e in enterprises:
            d = e.district or "未知区"
            district_ents[d].append(e)
        partitions = {
            d: {"parts": [d], "enterprises": ents}
            for d, ents in sorted(district_ents.items())
        }

    print(f"\nPartitions ({len(partitions)}):")
    for name, info in partitions.items():
        parts_str = ", ".join(info["parts"])
        print(f"  {name}: {len(info['enterprises'])} enterprises (parts: {parts_str})")

    provider = args.provider or get_map_provider()
    key = args.key or get_map_key(provider, "")
    if not key and not args.skip_geocode:
        print(f"\nWARNING: No API key for provider '{provider}'. Geocoding will likely fail.")
        print("  Set GAODE_API_KEY / TENCENT_MAP_KEY env var or pass --key.")

    reports = []

    for partition_name, info in partitions.items():
        parts = info["parts"]
        ents = info["enterprises"]

        # Apply alias if provided
        display_name = _apply_alias(partition_name, aliases)

        district_label = display_name
        district_parts = parts if len(parts) > 1 else None

        # Generate config
        config_path = generate_config(
            city=args.city,
            district=district_label,
            year=args.year,
            enterprises=ents,
            output_dir=args.output_dir,
            district_parts=district_parts,
        )

        # Ensure cache_file points to shared city cache
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        cache_abs = os.path.abspath(args.cache)
        config["gaode"]["cache_file"] = cache_abs
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(config, f, default_flow_style=False,
                      allow_unicode=True, sort_keys=False, width=200)

        print(f"\n{'='*60}")
        print(f"Partition: {display_name} ({len(ents)} enterprises)")
        print(f"Config: {config_path}")
        print(f"Cache:  {cache_abs}")
        print(f"{'='*60}")

        # Step 1: Geocode
        if not args.skip_geocode:
            print("\n-- Geocoding --")
            geo_result = run_geocode_for_config(config_path, key, provider)
            print(geo_result.stdout)
            if geo_result.returncode != 0:
                print(f"Geocode stderr: {geo_result.stderr}")
        else:
            print("\n-- Skipping geocode (use existing cache) --")

        # Step 2: Create map
        if not args.skip_map:
            print("\n-- Creating map --")
            map_result = run_create_map_for_config(config_path)
            print(map_result.stdout)
            if map_result.returncode != 0:
                print(f"Map stderr: {map_result.stderr}")
            map_ok = map_result.returncode == 0
        else:
            print("\n-- Skipping map generation --")
            map_ok = None

        # Report
        geocoded = sum(1 for e in ents if e.lat is not None and e.lon is not None)
        reports.append({
            "partition": display_name,
            "parts": parts,
            "total": len(ents),
            "geocoded": geocoded,
            "config": config_path,
            "output": config["meta"]["output_path"],
            "map_ok": map_ok,
        })

    # Summary
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    total_all = sum(r["total"] for r in reports)
    total_geo = sum(r["geocoded"] for r in reports)
    print(f"Total partitions:  {len(reports)}")
    print(f"Total enterprises: {total_all}")
    print(f"Total geocoded:    {total_geo} ({total_geo/total_all*100:.1f}%)")
    print("")
    for r in reports:
        status = "OK " if r["map_ok"] else "ERR" if r["map_ok"] is False else "---"
        parts_display = f" ({'+'.join(r['parts'])})" if len(r["parts"]) > 1 else ""
        print(f"  [{status}] {r['partition']}{parts_display}")
        print(f"       {r['geocoded']}/{r['total']} geocoded -> {r['output']}")
    print(f"{'='*60}")

    return 0


if __name__ == "__main__":
    exit(main())
