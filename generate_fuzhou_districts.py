#!/usr/bin/env python3
"""Generate district YAMLs from Fuzhou 2026 HTML data and merge into 主城区."""

import yaml
import copy
from bs4 import BeautifulSoup
from collections import defaultdict
from pathlib import Path

DATA_DIR = Path("data/福州")
SKILL_DIR = Path(".claude/skills/create-pollution-map")


def load_yaml(path):
    with open(path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def save_yaml(path, data):
    with open(path, 'w', encoding='utf-8') as f:
        yaml.dump(data, f, allow_unicode=True, sort_keys=False, default_flow_style=False)


def parse_html():
    """Parse HTML tables and return enterprises grouped by district."""
    with open('/tmp/fuzhou_2026.html', 'r', encoding='utf-8') as f:
        soup = BeautifulSoup(f.read(), 'html.parser')

    tables = soup.find_all('table')
    table_categories = {0: '水环境', 1: '地下水', 2: '大气环境', 3: '噪声', 4: '土壤污染', 5: '环境风险'}
    target_districts = {'鼓楼区', '台江区', '晋安区', '仓山区', '马尾区'}

    enterprises = defaultdict(lambda: {'district': '', 'categories': set()})
    for ti, table in enumerate(tables):
        category = table_categories.get(ti, '未分类')
        for row in table.find_all('tr')[1:]:
            cells = row.find_all(['td', 'th'])
            if len(cells) < 3:
                continue
            texts = [c.get_text(strip=True) for c in cells]
            district = texts[1]
            name = texts[2]
            if district not in target_districts:
                continue
            enterprises[name]['district'] = district
            enterprises[name]['categories'].add(category)

    by_district = defaultdict(list)
    for name, data in enterprises.items():
        by_district[data['district']].append({
            'name': name,
            'categories': sorted(data['categories']),
        })
    return by_district


def build_base_config(district_name, enterprise_count):
    """Load defaults.yaml and customize for district."""
    defaults = load_yaml(SKILL_DIR / 'defaults.yaml')
    config = copy.deepcopy(defaults)

    district_short = district_name.replace('区', '')
    config['meta']['title'] = f'福州{district_short}区2026年重点污染源单位地理分布图'
    config['meta']['subtitle'] = (
        f'数据来源：福州生态环境局2026年度环境监管重点单位名录  |  '
        f'共{enterprise_count}家  |  坐标：高德地图'
    )
    config['meta']['output_path'] = f'./福州_{district_short}区_2026_output.png'
    config['gaode']['cache_file'] = str(DATA_DIR / 'geocode_cache_福州.json')
    config['gaode']['city'] = '福州'
    config['render_mode'] = 'risk'
    config['risk_scoring']['enabled'] = False
    config['risk_scoring']['auto_assign'] = True

    return config


def build_enterprise_entry(name, categories, address=None):
    """Build a single enterprise entry."""
    entry = {
        'name': name,
        'address': address or f'福州{name}',
        'label': name,
        'data_source': '福州生态环境局2026年度环境监管重点单位名录',
        'source_type': 'official',
        'data_date': '2026-03',
        'categories': categories,
    }
    return entry


def merge_with_existing(html_ents, existing_yaml_path):
    """Merge HTML data with existing YAML, preserving geocoded coordinates."""
    if not existing_yaml_path.exists():
        return [build_enterprise_entry(e['name'], e['categories']) for e in html_ents]

    existing = load_yaml(existing_yaml_path)
    existing_by_name = {}
    for e in existing.get('enterprises', []):
        existing_by_name[e['name']] = e

    merged = []
    for he in html_ents:
        name = he['name']
        if name in existing_by_name:
            # Update categories, preserve other fields
            existing_ent = copy.deepcopy(existing_by_name[name])
            existing_ent['categories'] = he['categories']
            # Remove old risk fields if they exist (will be recomputed)
            existing_ent.pop('risk_level', None)
            existing_ent.pop('risk_score', None)
            existing_ent.pop('risk_factors', None)
            merged.append(existing_ent)
        else:
            # New enterprise from HTML
            merged.append(build_enterprise_entry(name, he['categories']))
    return merged


def generate_district_yaml(district_full, html_ents, existing_path=None):
    """Generate a district YAML config."""
    config = build_base_config(district_full, len(html_ents))

    if existing_path and existing_path.exists():
        enterprises = merge_with_existing(html_ents, existing_path)
    else:
        enterprises = [build_enterprise_entry(e['name'], e['categories']) for e in html_ents]

    config['enterprises'] = enterprises
    return config


def main():
    by_district = parse_html()

    district_files = {
        '鼓楼区': DATA_DIR / '福州_鼓楼区_2026.yaml',
        '台江区': DATA_DIR / '福州_台江区_2026.yaml',
        '晋安区': DATA_DIR / '福州_晋安区_2026.yaml',
        '仓山区': DATA_DIR / '福州_仓山区_2026.yaml',
        '马尾区': DATA_DIR / '福州_马尾区_2026.yaml',
    }

    # Generate/update individual district YAMLs
    for district, filepath in district_files.items():
        html_ents = by_district.get(district, [])
        if not html_ents:
            print(f"Warning: no enterprises found for {district}")
            continue

        config = generate_district_yaml(district, html_ents, filepath if filepath.exists() else None)
        save_yaml(filepath, config)
        print(f"Generated {filepath} with {len(config['enterprises'])} enterprises")

    # Build merged 主城区 config
    all_enterprises = []
    for district in ['鼓楼区', '台江区', '晋安区', '仓山区', '马尾区']:
        html_ents = by_district.get(district, [])
        for he in html_ents:
            ent = build_enterprise_entry(he['name'], he['categories'])
            ent['district'] = district.replace('区', '')
            all_enterprises.append(ent)

    # Read existing coordinates from individual YAMLs and merge
    for district, filepath in district_files.items():
        if not filepath.exists():
            continue
        existing = load_yaml(filepath)
        for e in existing.get('enterprises', []):
            if 'lat' in e and 'lon' in e:
                for ae in all_enterprises:
                    if ae['name'] == e['name']:
                        ae['lat'] = e['lat']
                        ae['lon'] = e['lon']
                        ae['geocode_level'] = e.get('geocode_level', '')
                        ae['address'] = e.get('address', ae['address'])
                        break

    main_config = build_base_config('主城区', len(all_enterprises))
    main_config['meta']['title'] = '福州主城区2026年重点污染源单位地理分布图'
    main_config['meta']['subtitle'] = (
        f'数据来源：福州生态环境局2026年度环境监管重点单位名录  |  '
        f'共{len(all_enterprises)}家  |  鼓楼{len(by_district.get("鼓楼区", []))}家·'
        f'台江{len(by_district.get("台江区", []))}家·晋安{len(by_district.get("晋安区", []))}家·'
        f'仓山{len(by_district.get("仓山区", []))}家·马尾{len(by_district.get("马尾区", []))}家'
    )
    main_config['meta']['output_path'] = './福州_主城区_2026_output.png'
    main_config['enterprises'] = all_enterprises

    main_path = DATA_DIR / '福州_主城区_2026.yaml'
    save_yaml(main_path, main_config)
    print(f"Generated {main_path} with {len(all_enterprises)} enterprises")

    # Summary
    print(f"\nSummary:")
    for district in ['鼓楼区', '台江区', '晋安区', '仓山区', '马尾区']:
        print(f"  {district}: {len(by_district.get(district, []))} enterprises")
    print(f"  主城区总计: {len(all_enterprises)} enterprises")


if __name__ == '__main__':
    main()
