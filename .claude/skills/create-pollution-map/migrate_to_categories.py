#!/usr/bin/env python3
"""
Migrate existing YAML configs from single-category entries to multi-category entries.

For each enterprise, if the same name appears multiple times with different categories,
merge them into a single entry with a `categories` list.

Preserves the most precise coordinates (POI > 门址 > 兴趣点 > 区县/乡镇/村庄).
Removes the old `category` field in favor of `categories`.
"""

import argparse
import os
import yaml


GEOCODE_PRECISION_ORDER = {
    'POI': 0,
    '门址': 1,
    '兴趣点': 2,
    '区县': 3,
    '乡镇': 4,
    '村庄': 5,
    '未知': 6,
}


def _precision_rank(level):
    return GEOCODE_PRECISION_ORDER.get(level, 999)


def migrate_config(config: dict) -> dict:
    """Merge enterprises by name, collecting all categories."""
    enterprises = config.get('enterprises', [])

    # Group by name
    groups = {}
    for e in enterprises:
        name = e['name']
        if name not in groups:
            groups[name] = []
        groups[name].append(e)

    merged = []
    for name, entries in groups.items():
        if len(entries) == 1:
            # Single entry: just convert category to categories
            e = dict(entries[0])
            cat = e.pop('category', None)
            if cat:
                e['categories'] = [cat]
            else:
                e['categories'] = e.get('categories', ['未分类'])
            merged.append(e)
            continue

        # Multiple entries: merge categories, keep best coords
        all_cats = set()
        best_entry = None
        best_rank = 999

        for e in entries:
            cat = e.get('category')
            if cat:
                all_cats.add(cat)
            # Also check for existing categories list
            for c in e.get('categories', []):
                all_cats.add(c)

            # Pick best coordinates
            level = e.get('geocode_level', '未知')
            rank = _precision_rank(level)
            if rank < best_rank:
                best_rank = rank
                best_entry = e

        # Fallback: if no best_entry found, use the first entry
        if best_entry is None:
            best_entry = entries[0]

        # Build merged entry from best_entry
        merged_e = dict(best_entry)
        merged_e.pop('category', None)
        merged_e['categories'] = sorted(all_cats)
        merged.append(merged_e)

    config['enterprises'] = merged

    # Update subtitle count
    meta = config.get('meta', {})
    if 'subtitle' in meta:
        subtitle = meta['subtitle']
        # Try to update the count in subtitle like "共{count}家"
        import re
        meta['subtitle'] = re.sub(r'共\d+家', f'共{len(merged)}家', subtitle)

    return config


def migrate_file(filepath: str):
    with open(filepath, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)

    old_count = len(config.get('enterprises', []))
    config = migrate_config(config)
    new_count = len(config.get('enterprises', []))

    with open(filepath, 'w', encoding='utf-8') as f:
        yaml.dump(config, f, allow_unicode=True, sort_keys=False, default_flow_style=False)

    print(f"Migrated {filepath}: {old_count} → {new_count} entries (merged {old_count - new_count} duplicates)")


def main():
    parser = argparse.ArgumentParser(description='Migrate YAML configs to multi-category format')
    parser.add_argument('files', nargs='+', help='YAML config files to migrate')
    args = parser.parse_args()

    for filepath in args.files:
        if os.path.exists(filepath):
            migrate_file(filepath)
        else:
            print(f"SKIP: file not found: {filepath}")


if __name__ == '__main__':
    main()
