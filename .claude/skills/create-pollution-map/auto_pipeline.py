#!/usr/bin/env python3
"""
Auto-pipeline: orchestrate data collection → YAML generation → geocode → map.

Designed to be called by Claude Code after it discovers data source URLs via
WebSearch. The pipeline extracts enterprise data, enriches addresses, generates
a config YAML, runs geocoding, and produces the final map image.

Usage:
    # Full pipeline with discovered URLs
    python auto_pipeline.py \
        --city 福州 --district 仓山区 --year 2026 \
        --urls official:https://... exposure:https://...

    # Extract-only (generate YAML, skip geocode/map)
    python auto_pipeline.py \
        --city 福州 --district 仓山区 --year 2026 \
        --urls official:https://... \
        --extract-only

    # From existing JSON (skip extraction)
    python auto_pipeline.py \
        --city 福州 --district 仓山区 --year 2026 \
        --input ./enterprises.json

    # Auto-confirm (skip interactive confirmation)
    python auto_pipeline.py ... --auto-confirm
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from collect_data import (
    DataExtractor, AddressEnricher, Source, Enterprise,
    merge_sources, generate_config, get_gaode_key,
)


def parse_url_args(url_args: list) -> list[Source]:
    """Parse --urls arguments like ['official:https://...', 'exposure:https://...'].

    Also supports format suffix: official:html:https://... or official:pdf:https://...
    """
    sources = []
    for arg in url_args:
        # Handle URLs with https:// which contain colons
        # Format: type:url  OR  type:format:url
        # e.g. official:https://www.xxx.gov.cn/...
        # e.g. official:pdf:https://www.xxx.gov.cn/.../file.pdf
        parts = arg.split(":", 2)

        if len(parts) == 2:
            source_type, url = parts
            fmt = "pdf" if url.lower().endswith(".pdf") else "html"
        elif len(parts) == 3:
            # Could be type:format:url or type:https://url (where https got split)
            if parts[1] in ("http", "https"):
                # This is type:http://rest-of-url
                source_type = parts[0]
                url = parts[1] + ":" + parts[2]
                fmt = "pdf" if url.lower().endswith(".pdf") else "html"
            else:
                # This is type:format:url
                source_type, fmt, url = parts
        else:
            print(f"WARNING: Invalid URL format '{arg}', expected type:url or type:format:url")
            continue

        sources.append(Source(
            url=url,
            source_type=source_type,
            format=fmt,
        ))
    return sources


def extract_all(sources: list[Source]) -> dict:
    """Extract enterprises from all sources.
    Returns dict mapping source_type -> list of Enterprise.
    """
    extractor = DataExtractor()
    results = {}

    for source in sources:
        print(f"\n{'='*60}")
        print(f"Extracting from {source.source_type}: {source.url}")
        print(f"Format: {source.format}")
        print("=" * 60)

        enterprises = extractor.extract(source)

        if source.source_type not in results:
            results[source.source_type] = []
        results[source.source_type].extend(enterprises)

        print(f"  -> Extracted {len(enterprises)} enterprises")

    return results


def print_summary(results: dict, enriched_report: dict = None) -> bool:
    """Print extraction summary and ask for user confirmation.
    Returns True if user wants to proceed.
    """
    print("\n" + "=" * 60)
    print("数据采集结果汇总")
    print("=" * 60)

    total = 0
    for source_type, enterprises in results.items():
        total += len(enterprises)
        print(f"\n  [{source_type}] {len(enterprises)} 家企业")
        for i, ent in enumerate(enterprises[:5], 1):
            cat_str = f" [{ent.category}]" if ent.category else ""
            print(f"    {i}. {ent.name}{cat_str}")
        if len(enterprises) > 5:
            print(f"       ... 共 {len(enterprises)} 家")

    if enriched_report:
        print(f"\n  地址补全: 成功 {len(enriched_report['fixed'])} 家, "
              f"失败 {len(enriched_report['failed'])} 家, "
              f"跳过 {len(enriched_report['skipped'])} 家")

    print(f"\n  总计: {total} 家企业")
    print("=" * 60)
    return True


def run_geocode(config_path: str) -> int:
    """Run geocode.py on the generated config."""
    print(f"\n{'='*60}")
    print("Running geocode.py...")
    print("=" * 60)

    cmd = [sys.executable, "geocode.py", "-c", config_path]
    result = subprocess.run(cmd, cwd=os.path.dirname(__file__))
    return result.returncode


def run_create_map(config_path: str) -> int:
    """Run create_map.py on the generated config."""
    print(f"\n{'='*60}")
    print("Running create_map.py...")
    print("=" * 60)

    cmd = [sys.executable, "create_map.py", "-c", config_path]
    result = subprocess.run(cmd, cwd=os.path.dirname(__file__))
    return result.returncode


def main():
    parser = argparse.ArgumentParser(
        description="Auto-pipeline: collect → YAML → geocode → map"
    )
    parser.add_argument("--city", required=True, help="City name, e.g. 福州")
    parser.add_argument("--district", required=True, help="District name, e.g. 仓山区")
    parser.add_argument("--year", type=int, required=True, help="Year, e.g. 2026")
    parser.add_argument(
        "--urls", nargs="+", metavar="TYPE:URL",
        help="Data source URLs, e.g. official:https://... exposure:https://..."
    )
    parser.add_argument(
        "--input", "-i", help="Skip extraction, load enterprises from JSON file"
    )
    parser.add_argument(
        "--output-dir", "-o", default="./data",
        help="Output directory for generated files"
    )
    parser.add_argument(
        "--extract-only", action="store_true",
        help="Stop after generating YAML config (skip geocode + map)"
    )
    parser.add_argument(
        "--skip-enrich", action="store_true",
        help="Skip address enrichment via Gaode POI"
    )
    parser.add_argument(
        "--auto-confirm", action="store_true",
        help="Skip interactive confirmation, proceed automatically"
    )
    parser.add_argument(
        "--key", default="",
        help="Gaode API key (or set GAODE_API_KEY env var)"
    )
    args = parser.parse_args()

    base_dir = os.path.dirname(os.path.abspath(__file__))
    output_dir = os.path.join(base_dir, args.output_dir)
    os.makedirs(output_dir, exist_ok=True)

    # ------------------------------------------------------------------
    # Step 1: Extract data
    # ------------------------------------------------------------------
    if args.input:
        print(f"\nLoading enterprises from {args.input}")
        with open(args.input, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Convert dicts back to Enterprise objects
        all_enterprises = [Enterprise(**item) for item in data]
        results = {"manual": all_enterprises}
    elif args.urls:
        sources = parse_url_args(args.urls)
        if not sources:
            print("ERROR: No valid URLs provided")
            return 1
        results = extract_all(sources)
    else:
        print("ERROR: Must provide either --urls or --input")
        return 1

    if not any(results.values()):
        print("ERROR: No enterprises extracted from any source")
        return 1

    # ------------------------------------------------------------------
    # Step 2: Merge and deduplicate
    # ------------------------------------------------------------------
    print(f"\n{'='*60}")
    print("Merging and deduplicating...")
    print("=" * 60)
    all_enterprises = merge_sources(results)
    print(f"  -> {sum(len(v) for v in results.values())} raw -> {len(all_enterprises)} unique")

    # ------------------------------------------------------------------
    # Step 2.5: Filter by target district
    # ------------------------------------------------------------------
    target_district = args.district
    if target_district:
        print(f"\n{'='*60}")
        print(f"Filtering for district: {target_district}")
        print("=" * 60)
        before_count = len(all_enterprises)
        # Filter: keep enterprises whose district field matches target district,
        # or if district field is empty, keep them (might be from district-level page)
        filtered = []
        for ent in all_enterprises:
            if ent.district and ent.district != target_district:
                continue
            filtered.append(ent)
        all_enterprises = filtered
        print(f"  -> {before_count} -> {len(all_enterprises)} enterprises in {target_district}")

    # ------------------------------------------------------------------
    # Step 3: Enrich addresses
    # ------------------------------------------------------------------
    enriched_report = None
    if not args.skip_enrich:
        key = args.key or get_gaode_key("")
        if key:
            enricher = AddressEnricher(key=key, city=args.city)
            print(f"\n{'='*60}")
            print("Enriching addresses via Gaode POI...")
            print("=" * 60)
            enriched_report = enricher.enrich(all_enterprises)
            print(f"  -> Fixed: {len(enriched_report['fixed'])}, "
                  f"Failed: {len(enriched_report['failed'])}, "
                  f"Skipped: {len(enriched_report['skipped'])}")
        else:
            print("\nWARNING: No Gaode API key. Skipping address enrichment.")
            print("  Set GAODE_API_KEY or pass --key")

    # ------------------------------------------------------------------
    # Step 4: User confirmation
    # ------------------------------------------------------------------
    if not args.auto_confirm:
        print_summary(results, enriched_report)
        # In non-interactive mode (e.g. when called by Claude Code), auto-confirm
        # We detect this by checking if stdin is a tty
        if sys.stdin.isatty():
            try:
                response = input("\nProceed with YAML generation? (y/n): ").strip().lower()
                if response not in ("y", "yes"):
                    print("Aborted by user.")
                    return 0
            except EOFError:
                # Non-interactive, proceed
                pass

    # ------------------------------------------------------------------
    # Step 5: Generate YAML config
    # ------------------------------------------------------------------
    print(f"\n{'='*60}")
    print("Generating YAML config...")
    print("=" * 60)

    config_path = generate_config(
        city=args.city,
        district=args.district,
        year=args.year,
        enterprises=all_enterprises,
        output_dir=output_dir,
    )
    print(f"  -> Config saved: {config_path}")

    if args.extract_only:
        print("\n--extract-only specified. Stopping here.")
        print(f"Next steps:")
        print(f"  1. Review and edit: {config_path}")
        print(f"  2. Run geocode: python3 geocode.py -c {config_path}")
        print(f"  3. Generate map: python3 create_map.py -c {config_path}")
        return 0

    # ------------------------------------------------------------------
    # Step 6: Geocode
    # ------------------------------------------------------------------
    geocode_rc = run_geocode(config_path)
    if geocode_rc != 0:
        print("\nWARNING: geocode.py returned non-zero. Some addresses may need fixing.")
        print(f"  Run: python3 fix_addresses.py -c {config_path} --apply")

    # ------------------------------------------------------------------
    # Step 7: Generate map
    # ------------------------------------------------------------------
    map_rc = run_create_map(config_path)
    if map_rc != 0:
        print("\nERROR: create_map.py failed.")
        return 1

    print(f"\n{'='*60}")
    print("Pipeline complete!")
    print("=" * 60)
    print(f"Config: {config_path}")
    return 0


if __name__ == "__main__":
    exit(main())
