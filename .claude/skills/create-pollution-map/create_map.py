#!/usr/bin/env python3
"""
Generate high-resolution pollution-source distribution map with basemap.

Usage:
    python create_map.py -c config.yaml
"""

import argparse
import json
import math
import os
import sys
import warnings
from pathlib import Path

import contextily as ctx
import geopandas as gpd
import matplotlib
import matplotlib.pyplot as plt
import yaml
from PIL import Image, ImageDraw
from shapely.geometry import Point, Polygon

sys.path.insert(0, os.path.dirname(__file__))
from utils import (get_font, resolve_image_path, resolve_cache_path, ensure_dir,
                   get_city_from_config, load_config_with_defaults, estimate_city_bounds,
                   coord_in_bounds)

matplotlib.use('Agg')
warnings.filterwarnings('ignore')

plt.rcParams['font.family'] = ['Arial Unicode MS', 'PingFang HK', 'sans-serif']
plt.rcParams['axes.unicode_minus'] = False


def load_config(path: str) -> dict:
    config = load_config_with_defaults(path)
    # Resolve relative paths against config file's directory, not CWD,
    # and route dynamic outputs into data/<city>/ automatically.
    config_dir = os.path.dirname(os.path.abspath(path))
    config['meta']['output_path'] = resolve_image_path(
        config, config_dir, config['meta']['output_path']
    )
    cache = config['gaode'].get('cache_file', './geocode_cache.json')
    config['gaode']['cache_file'] = resolve_cache_path(config, config_dir, cache)
    return config


def _cache_lookup(cache: dict, name: str, addr: str):
    """Lookup cache entry by name|addr key (geocode.py format) or addr-only key."""
    key = f"{name}|{addr}"
    if key in cache:
        return cache[key]
    if addr in cache:
        return cache[addr]
    return None


def merge_coords(enterprises: list, cache_file: str) -> list:
    """Merge lat/lon + geocode level from cache into enterprise records."""
    cache = {}
    if os.path.exists(cache_file):
        with open(cache_file, 'r', encoding='utf-8') as f:
            cache = json.load(f)

    merged = []
    for e in enterprises:
        rec = dict(e)
        name = rec['name']
        addr = rec['address']
        cached = _cache_lookup(cache, name, addr)

        if 'lat' in rec and 'lon' in rec:
            if cached:
                rec['geocode_level'] = cached.get('level', '未知')
            else:
                rec['geocode_level'] = rec.get('geocode_level', '未知')
        elif cached:
            rec['lat'] = cached['lat']
            rec['lon'] = cached['lon']
            rec['geocode_level'] = cached.get('level', '未知')
        else:
            print(f"WARNING: no coords for '{name}' ({addr}), skipping")
            continue
        merged.append(rec)
    return merged


def validate_data_sources(enterprises: list) -> dict:
    """Check data_source coverage and print summary."""
    missing = []
    source_summary = {}
    date_summary = {}
    for e in enterprises:
        name = e['name']
        src = e.get('data_source', '').strip()
        date = e.get('data_date', '').strip()
        if not src:
            missing.append(name)
        else:
            source_summary[src] = source_summary.get(src, 0) + 1
            if date:
                date_summary[src] = date

    print("\n" + "=" * 50)
    print("数据来源覆盖检查")
    print("=" * 50)
    if missing:
        print(f"\n⚠ WARNING: {len(missing)}/{len(enterprises)} 家企业缺少 data_source:")
        for name in missing:
            print(f"   - {name}")
    else:
        print(f"\n✓ 全部 {len(enterprises)} 家企业均有 data_source")

    print("\n数据来源分布:")
    for src, count in sorted(source_summary.items(), key=lambda x: -x[1]):
        d = date_summary.get(src, '')
        d_str = f"  ({d})" if d else ""
        print(f"   [{count}家] {src}{d_str}")
    print("=" * 50)
    return source_summary


def validate_coord_reasonableness(enterprises: list, city: str = "") -> tuple:
    """Check if coordinates are reasonable: within expected city bounds and not extreme outliers.

    Returns (out_of_bounds_count, outlier_count).
    """
    if len(enterprises) < 2:
        return 0, 0

    valid_ents = [e for e in enterprises if e.get('lat') is not None and e.get('lon') is not None]
    lats = [e['lat'] for e in valid_ents]
    lons = [e['lon'] for e in valid_ents]

    # Auto-infer bounds from enterprise coords or city center lookup
    bounds = estimate_city_bounds(city, valid_ents)

    # 1. Check all coords are within city rough bounds
    out_of_bounds = []
    for e in valid_ents:
        lat, lon = e['lat'], e['lon']
        if not coord_in_bounds(lat, lon, bounds):
            out_of_bounds.append((e['name'], lat, lon))

    # 2. Outlier detection: points >3 std dev from centroid
    centroid_lat = sum(lats) / len(lats)
    centroid_lon = sum(lons) / len(lons)
    std_lat = (sum((x - centroid_lat) ** 2 for x in lats) / len(lats)) ** 0.5
    std_lon = (sum((x - centroid_lon) ** 2 for x in lons) / len(lons)) ** 0.5

    outliers = []
    for e in valid_ents:
        lat, lon = e['lat'], e['lon']
        d_lat = abs(lat - centroid_lat)
        d_lon = abs(lon - centroid_lon)
        # Use a threshold: >3 std dev in either dimension, or >30km from centroid
        if (d_lat > 3 * std_lat + 0.001 or d_lon > 3 * std_lon + 0.001):
            # Calculate approximate distance from centroid
            dist_m = ((d_lat * 111000) ** 2 + (d_lon * 111000 * math.cos(math.radians(centroid_lat))) ** 2) ** 0.5
            if dist_m > 50000:  # Only flag if >50km from centroid
                outliers.append((e['name'], lat, lon, dist_m, e.get('actual_address_verified', False)))

    verified_count = sum(1 for e in valid_ents if e.get('actual_address_verified'))

    if out_of_bounds or outliers:
        print("\n" + "=" * 50)
        print("坐标合理性检查")
        print("=" * 50)

        if out_of_bounds:
            print(f"\n🚨 严重错误: {len(out_of_bounds)} 家企业坐标超出{city or '合理'}范围：")
            for name, lat, lon in out_of_bounds:
                print(f"   - {name}: ({lat:.5f}, {lon:.5f})")

        if outliers:
            print(f"\n⚠️ 异常偏离: {len(outliers)} 家企业坐标距中心点>30km，可能是编码错误：")
            for name, lat, lon, dist, verified in outliers:
                tag = " [已核实实际经营地址]" if verified else " [可能为注册地址，需核实实际经营地址]"
                print(f"   - {name}: ({lat:.5f}, {lon:.5f}) 距中心{dist/1000:.1f}km{tag}")

        print("\n建议：使用 'python3 audit_coords.py --report -c config.yaml' 核对坐标")
        print("=" * 50)
    else:
        print("\n✓ 全部企业坐标在合理范围内，无异常偏离")
        if verified_count:
            print(f"  （其中 {verified_count} 家已人工核实为实际经营地址）")

    return len(out_of_bounds), len(outliers)


def _normalize_categories(enterprises):
    """Convert legacy single category to categories list."""
    for e in enterprises:
        if 'category' in e and 'categories' not in e:
            e['categories'] = [e['category']]
        elif 'categories' not in e:
            e['categories'] = ['未分类']


def validate_coords(enterprises: list) -> tuple:
    """Check geocode quality: low-precision levels and duplicate coordinates.

    Returns (low_precision_count, duplicate_group_count).
    """
    _normalize_categories(enterprises)

    low_precision_levels = {'区县', '乡镇', '未知', '公交地铁站点'}
    low_precision = []
    for e in enterprises:
        level = e.get('geocode_level', '未知')
        if level in low_precision_levels:
            low_precision.append((e['name'], level, e.get('address', '')))

    coord_map = {}
    duplicates = []
    for e in enterprises:
        if e.get('lat') is None or e.get('lon') is None:
            continue
        key = (round(e['lat'], 5), round(e['lon'], 5))
        if key in coord_map:
            # After migration, same name + same coords means merged entry is correct.
            # Different names at same coords are still potential duplicates.
            if coord_map[key] != e['name']:
                duplicates.append((coord_map[key], e['name'], key))
        else:
            coord_map[key] = e['name']

    dup_groups = 0
    if duplicates:
        seen = set()
        for a, b, _ in duplicates:
            pair = tuple(sorted([a, b]))
            if pair not in seen:
                seen.add(pair)
                dup_groups += 1

    if low_precision or duplicates:
        print("\n" + "=" * 50)
        print("坐标质量检查")
        print("=" * 50)

        if low_precision:
            print(f"\n⚠ WARNING: {len(low_precision)} 家企业地理编码精度不足，坐标可能为区域中心点：")
            for name, level, addr in low_precision:
                addr_hint = f" ({addr})" if addr else ""
                print(f"   - [{level}] {name}{addr_hint}")

        if duplicates:
            print(f"\n⚠ WARNING: 发现 {dup_groups} 组企业坐标完全相同（可能因地址模糊导致）：")
            seen = set()
            for a, b, (lat, lon) in duplicates:
                pair = tuple(sorted([a, b]))
                if pair not in seen:
                    seen.add(pair)
                    print(f"   - {a}  ↔  {b}  ({lat:.5f}, {lon:.5f})")

        print("\n建议：对精度不足的企业，通过高德地图网页版或实地确认准确地址后更新。")
        print("=" * 50)
    else:
        print("\n✓ 全部企业地理编码精度合格，无重复坐标")

    return len(low_precision), dup_groups


def validate_coords_in_boundary(enterprises: list, boundary_coords: list) -> int:
    """Check if all enterprise coordinates fall inside the district boundary polygon.

    Returns the number of enterprises found outside the boundary.
    """
    if not boundary_coords:
        return 0

    polygon = Polygon([(c[0], c[1]) for c in boundary_coords])
    outside = []
    for e in enterprises:
        point = Point(e['lon'], e['lat'])
        if not polygon.contains(point):
            outside.append(e)

    if outside:
        print("\n" + "=" * 50)
        print("行政区边界检查")
        print("=" * 50)
        print(f"\n🚨 严重错误: {len(outside)} 家企业坐标落在行政区边界外：")
        for e in outside:
            print(f"   - {e['name']}: ({e['lat']:.5f}, {e['lon']:.5f})")
        print("\n这些企业坐标明显不在目标行政区内，请运行 audit_coords.py 核实修正。")
        print("=" * 50)
    else:
        print("\n✓ 全部企业坐标位于行政区边界内")

    return len(outside)


def compute_auto_boundary(coords: list, padding_deg: float = 0.02) -> Polygon:
    """Compute bounding-box polygon from list of (lon, lat) with padding."""
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    return Polygon([
        (min(lons) - padding_deg, min(lats) - padding_deg),
        (max(lons) + padding_deg, min(lats) - padding_deg),
        (max(lons) + padding_deg, max(lats) + padding_deg),
        (min(lons) - padding_deg, max(lats) + padding_deg),
    ])


def _compute_label_offsets(coords, threshold_m=1200):
    """Assign directional offsets to labels to avoid overlap."""
    n = len(coords)
    directions = [
        (18, 18), (22, 0), (18, -18), (0, -22),
        (-18, -18), (-22, 0), (-18, 18), (0, 22),
    ]
    assigned = [-1] * n
    for i in range(n):
        xi, yi = coords[i]
        best_dir = 0
        min_conflicts = float('inf')
        for d in range(8):
            conflicts = sum(1 for j in range(i)
                            if assigned[j] == d
                            and ((coords[i][0] - coords[j][0]) ** 2 +
                                 (coords[i][1] - coords[j][1]) ** 2) ** 0.5 < threshold_m * 2)
            if conflicts < min_conflicts:
                min_conflicts = conflicts
                best_dir = d
                if conflicts == 0:
                    break
        assigned[i] = best_dir
    return [directions[d] for d in assigned]


# ---------------------------------------------------------------------------
# Decomposed rendering functions
# ---------------------------------------------------------------------------

def build_geodataframes(config: dict, enterprises: list):
    """Build boundary + enterprise GeoDataFrames in Web Mercator."""
    boundary_coords = config.get('boundary', {}).get('coords', [])
    if boundary_coords:
        boundary = Polygon([(c[0], c[1]) for c in boundary_coords])
    else:
        ec = [(e['lon'], e['lat']) for e in enterprises]
        boundary = compute_auto_boundary(ec)
        print(f"Auto-computed boundary from {len(ec)} enterprise coords")

    gdf_boundary = gpd.GeoDataFrame(
        {'name': ['District'], 'geometry': [boundary]}, crs='EPSG:4326'
    )
    _normalize_categories(enterprises)
    gdf = gpd.GeoDataFrame(
        {
            'name': [e['name'] for e in enterprises],
            'label': [e['label'] for e in enterprises],
            'categories': [e['categories'] for e in enterprises],
            'primary_category': [e['categories'][0] for e in enterprises],
            'geometry': [Point(e['lon'], e['lat']) for e in enterprises],
        },
        crs='EPSG:4326',
    )
    return gdf_boundary.to_crs(epsg=3857), gdf.to_crs(epsg=3857)


def render_basemap(gdf_boundary_wm, gdf_wm, config: dict) -> str:
    """Render matplotlib map with basemap, markers, labels, and risk zones."""
    meta = config['meta']
    map_cfg = config['map']
    cat_cfg = config['categories']

    fig = plt.figure(figsize=map_cfg.get('figsize', [18, 14]), dpi=meta.get('dpi', 200))
    ax = fig.add_axes([0.02, 0.02, 0.96, 0.88])

    bounds = gdf_boundary_wm.total_bounds
    padding = map_cfg.get('padding', 500)
    ax.set_xlim(bounds[0] - padding, bounds[2] + padding)
    ax.set_ylim(bounds[1] - padding, bounds[3] + padding)

    gaode_url = 'https://webrd01.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}'
    try:
        ctx.add_basemap(
            ax, source=gaode_url, alpha=map_cfg.get('basemap_alpha', 0.95),
            zoom=map_cfg.get('zoom', 13), interpolation='bilinear'
        )
    except Exception as e:
        print(f'Gaode tiles failed: {e}, trying fallback...')
        ctx.add_basemap(
            ax, source=ctx.providers.OpenStreetMap.Mapnik,
            alpha=map_cfg.get('basemap_alpha', 0.95), zoom=map_cfg.get('zoom', 13)
        )

    # Boundary outline
    boundary_geom = gdf_boundary_wm.geometry.iloc[0]
    if boundary_geom.geom_type == 'Polygon':
        x_b, y_b = boundary_geom.exterior.xy
        ax.plot(x_b, y_b, color='#1E90FF', linewidth=2.5, alpha=0.9, zorder=4)

    # Category-colored scatter points with shape differentiation
    color_map = {k: v['color'] for k, v in cat_cfg.items()}
    marker_map = {k: v.get('marker', 'o') for k, v in cat_cfg.items()}
    for cat, color in color_map.items():
        mask = gdf_wm['primary_category'] == cat
        if mask.any():
            marker = marker_map.get(cat, 'o')
            ax.scatter(
                gdf_wm.geometry.x[mask], gdf_wm.geometry.y[mask],
                c=color, s=280, marker=marker,
                edgecolors='white', linewidths=1.5, zorder=5, alpha=0.95
            )

    # Numbered labels with smart offsets
    wm_coords = list(zip(gdf_wm.geometry.x, gdf_wm.geometry.y))
    offsets = _compute_label_offsets(wm_coords, threshold_m=1200)

    for i, (xi, yi) in enumerate(wm_coords):
        dx, dy = offsets[i]
        if dx != 0 or dy != 0:
            ax.annotate(
                '', xy=(xi + dx * 0.5, yi + dy * 0.5), xytext=(xi, yi),
                arrowprops=dict(arrowstyle='-', color='white', lw=1.2, alpha=0.7),
                zorder=5
            )
        ax.annotate(
            str(i + 1), (xi, yi), textcoords='offset points', xytext=(dx, dy),
            ha='center', va='center', fontsize=13, fontweight='bold', color='black', zorder=6,
            bbox=dict(boxstyle='round,pad=0.2', facecolor='none', edgecolor='none', alpha=0)
        )

    # Risk zone buffers
    risk_cfg = config.get('risk_zones', {})
    if risk_cfg.get('enabled', False):
        radius = risk_cfg.get('radius_meters', 1000)
        buffers_gs = gpd.GeoSeries(gdf_wm.buffer(radius), crs='EPSG:3857')
        buffers_gs.plot(
            ax=ax, facecolor=risk_cfg.get('fill_color', '#FF4444'),
            alpha=risk_cfg.get('fill_alpha', 0.12), edgecolor='none', zorder=3
        )
        buffers_gs.boundary.plot(
            ax=ax, color=risk_cfg.get('edge_color', '#CC0000'),
            alpha=risk_cfg.get('edge_alpha', 0.25),
            linewidth=risk_cfg.get('edge_width', 0.8), zorder=3
        )

    ax.set_axis_off()

    # Map provider label in subtitle
    provider_name = '高德地图'
    subtitle = meta.get('subtitle', '').replace('{map_provider}', provider_name)
    if '坐标：' not in subtitle and '地图' not in subtitle:
        subtitle += f"  |  坐标：{provider_name}"

    fig.text(0.5, 0.95, meta['title'], ha='center', fontsize=20, fontweight='bold', color='#1a1a1a')
    fig.text(0.5, 0.93, subtitle, ha='center', fontsize=10, color='#666666')

    map_path = os.path.join(os.path.dirname(meta['output_path']) or '.', 'map_only.png')
    ensure_dir(map_path)
    fig.savefig(map_path, dpi=meta.get('dpi', 200), facecolor='white', bbox_inches='tight', pad_inches=0.1)
    plt.close()
    print(f'Map-only image saved: {map_path}')
    return map_path


def _blend_with_white(hex_color: str, alpha: float) -> str:
    """Blend a hex color with white background at given alpha (0-1)."""
    hex_color = hex_color.lstrip('#')
    r = int(hex_color[0:2], 16)
    g = int(hex_color[2:4], 16)
    b = int(hex_color[4:6], 16)
    nr = int(255 * (1 - alpha) + r * alpha)
    ng = int(255 * (1 - alpha) + g * alpha)
    nb = int(255 * (1 - alpha) + b * alpha)
    return f'#{nr:02x}{ng:02x}{nb:02x}'


def _draw_marker(draw, cx, cy, size, color, marker):
    """Draw a marker shape on PIL canvas. Supports: o, ^, s, D, *."""
    half = size // 2
    x0, y0 = cx - half, cy - half
    x1, y1 = cx + half, cy + half

    if marker == 's':
        draw.rectangle([(x0, y0), (x1, y1)], fill=color)
    elif marker == '^':
        draw.polygon([(cx, y0), (x1, y1), (x0, y1)], fill=color)
    elif marker == 'D':
        draw.polygon([(cx, y0), (x1, cy), (cx, y1), (x0, cy)], fill=color)
    elif marker == '*':
        # Simplified star: a circle with a small cross, visually distinctive
        draw.ellipse([(x0, y0), (x1, y1)], fill=color)
        draw.line([(cx, y0), (cx, y1)], fill='white', width=1)
        draw.line([(x0, cy), (x1, cy)], fill='white', width=1)
    else:
        # Default circle
        draw.ellipse([(x0, y0), (x1, y1)], fill=color)


def render_legend(enterprises: list, config: dict, map_width: int = 0) -> Image.Image:
    """Render PIL legend panel with category stats, enterprise list, and data sources.

    When enterprise count > 60, switches to horizontal layout (full-width, multi-column
    enterprise list) to avoid excessively tall images.
    """
    cat_cfg = config['categories']
    total_count = len(enterprises)
    horizontal_mode = total_count > 60 and map_width > 0

    if horizontal_mode:
        return _render_legend_horizontal(enterprises, config, map_width)
    return _render_legend_vertical(enterprises, config)


def _render_legend_vertical(enterprises: list, config: dict) -> Image.Image:
    """Original vertical legend for <=60 enterprises (placed on the right)."""
    cat_cfg = config['categories']
    legend_width = 650
    legend_height = 5000
    legend_img = Image.new('RGB', (legend_width, legend_height), 'white')
    draw = ImageDraw.Draw(legend_img)

    font_title = get_font(36)
    font_header = get_font(24)
    font_body = get_font(20)
    font_small = get_font(18)
    font_tiny = get_font(16)

    draw.rectangle([(0, 0), (legend_width - 1, legend_height - 1)], outline='#cccccc', width=2)

    margin = 30
    x = margin
    y = 40

    # Title
    bbox = draw.textbbox((0, 0), '图例', font=font_title)
    text_w = bbox[2] - bbox[0]
    draw.text(((legend_width - text_w) // 2, y), '图例', fill='#333333', font=font_title)
    y += 60

    # Total count
    total_count = len(enterprises)
    draw.text((x, y), '●', fill='#CC0000', font=font_header)
    draw.text((x + 35, y), f'重点污染源单位（{total_count}家）', fill='#333333', font=font_body)
    y += 50

    # Risk zone legend
    risk_cfg = config.get('risk_zones', {})
    if risk_cfg.get('enabled', False):
        rz_color = risk_cfg.get('fill_color', '#FF4444')
        rz_alpha = risk_cfg.get('fill_alpha', 0.12)
        blended = _blend_with_white(rz_color, rz_alpha)
        draw.ellipse([(x, y - 2), (x + 28, y + 26)], fill=blended)
        draw.text((x + 38, y), '环境潜在压力区（1km缓冲重叠）', fill='#333333', font=font_body)
        y += 32

    # Category stats
    y += 15
    draw.text((x, y), '【分类统计】', fill='#333333', font=font_header)
    y += 40

    cats_count = {}
    for e in enterprises:
        for cat in e['categories']:
            cats_count[cat] = cats_count.get(cat, 0) + 1

    for cat_key, cat_info in cat_cfg.items():
        if cat_key in cats_count:
            marker = cat_info.get('marker', 'o')
            _draw_marker(draw, x + 10, y + 12, 18, cat_info['color'], marker)
            draw.text((x + 30, y), f"{cat_info['display']}：{cats_count[cat_key]}家", fill='#333333', font=font_body)
            y += 32

    # Enterprise list
    y += 33
    draw.text((x, y), '【企业列表】', fill='#333333', font=font_header)
    y += 40

    # Build source index
    circled_nums = ['①', '②', '③', '④', '⑤', '⑥', '⑦', '⑧', '⑨', '⑩']
    source_list = []
    source_index = {}
    for e in enterprises:
        src = e.get('data_source', '').strip()
        if src and src not in source_index:
            idx = len(source_list)
            source_index[src] = circled_nums[idx] if idx < len(circled_nums) else f'[{idx+1}]'
            source_list.append(src)

    for i, e in enumerate(enterprises):
        lines = e['label'].split('\n')
        cat_keys = e['categories']
        num_color = cat_cfg.get(cat_keys[0], {}).get('color', '#CC0000')
        src_ref = source_index.get(e.get('data_source', '').strip(), '')

        draw.text((x, y), f'{i + 1}', fill=num_color, font=font_small)
        draw.text((x + 28, y), lines[0], fill='#333333', font=font_small)
        y += 26

        for line in lines[1:]:
            draw.text((x + 28, y), line, fill='#555555', font=font_tiny)
            y += 22

        display_names = [cat_cfg.get(c, {}).get('display', c) for c in cat_keys]
        tag = '【' + '】【'.join(display_names) + '】'
        if src_ref:
            tag += f' {src_ref}'
        draw.text((x + 28, y), tag, fill='#888888', font=font_tiny)
        y += 26

    # Source type grouping
    source_type_labels = {
        'official': '官方监管名录',
        'license': '许可证信息',
        'complaint': '群众投诉/信访',
        'exposure': '违规曝光',
    }
    source_type_counts = {}
    for e in enterprises:
        st = e.get('source_type', '').strip()
        if st:
            source_type_counts[st] = source_type_counts.get(st, 0) + 1

    if source_type_counts:
        y += 25
        draw.text((x, y), '数据来源分布：', fill='#888888', font=font_tiny)
        y += 20
        for st, count in sorted(source_type_counts.items(), key=lambda x: -x[1]):
            label = source_type_labels.get(st, st)
            draw.text((x, y), f'  [{count}家] {label}', fill='#888888', font=font_tiny)
            y += 18

    # Data source footer
    y += 25
    draw.text((x, y), '数据来源:', fill='#888888', font=font_tiny)
    y += 20
    for src in source_list:
        ref = source_index[src]
        draw.text((x, y), f'  {ref} {src}', fill='#888888', font=font_tiny)
        y += 18

    # Conditional radiation footnote
    has_radiation = any('辐射安全' in e.get('categories', []) for e in enterprises)
    if has_radiation:
        y += 12
        draw.text((x, y), '注：辐射单位为射线装置使用单位', fill='#888888', font=font_tiny)
        y += 20

    # Crop to actual content
    actual_height = y
    legend_img = legend_img.crop((0, 0, legend_width, actual_height))
    draw_final = ImageDraw.Draw(legend_img)
    draw_final.rectangle([(0, 0), (legend_width - 1, actual_height - 1)], outline='#cccccc', width=2)

    legend_path = os.path.join(os.path.dirname(config['meta']['output_path']) or '.', 'legend_only.png')
    ensure_dir(legend_path)
    legend_img.save(legend_path)
    print(f'Legend-only image saved: {legend_path}')
    return legend_img


def _render_legend_horizontal(enterprises: list, config: dict, map_width: int) -> Image.Image:
    """Horizontal legend for >60 enterprises (placed below the map).

    Layout: title + stats on top, enterprise list in multi-column grid below.
    """
    cat_cfg = config['categories']
    legend_width = map_width
    legend_height = 2000  # initial buffer, will crop
    legend_img = Image.new('RGB', (legend_width, legend_height), 'white')
    draw = ImageDraw.Draw(legend_img)

    font_title = get_font(32)
    font_header = get_font(22)
    font_body = get_font(18)
    font_small = get_font(17)
    font_tiny = get_font(15)

    margin = 30
    x = margin
    y = 30

    # Title (left-aligned)
    total_count = len(enterprises)
    draw.text((x, y), '图例', fill='#333333', font=font_title)
    y += 50

    # Total count + risk zone in one row
    draw.text((x, y), '●', fill='#CC0000', font=font_header)
    draw.text((x + 30, y), f'重点污染源单位（{total_count}家）', fill='#333333', font=font_body)

    risk_cfg = config.get('risk_zones', {})
    if risk_cfg.get('enabled', False):
        rz_color = risk_cfg.get('fill_color', '#FF4444')
        rz_alpha = risk_cfg.get('fill_alpha', 0.12)
        blended = _blend_with_white(rz_color, rz_alpha)
        draw.ellipse([(x + 320, y - 2), (x + 348, y + 26)], fill=blended)
        draw.text((x + 355, y), '环境潜在压力区（1km缓冲重叠）', fill='#333333', font=font_body)
    y += 40

    # Category stats (horizontal row)
    cats_count = {}
    for e in enterprises:
        for cat in e['categories']:
            cats_count[cat] = cats_count.get(cat, 0) + 1

    cat_x = x
    for cat_key, cat_info in cat_cfg.items():
        if cat_key in cats_count:
            marker = cat_info.get('marker', 'o')
            _draw_marker(draw, cat_x + 8, y + 10, 16, cat_info['color'], marker)
            draw.text((cat_x + 24, y), f"{cat_info['display']}：{cats_count[cat_key]}家", fill='#333333', font=font_body)
            # estimate text width and advance
            bbox = draw.textbbox((0, 0), f"{cat_info['display']}：{cats_count[cat_key]}家", font=font_body)
            cat_x += (bbox[2] - bbox[0]) + 40
            if cat_x > legend_width - 200:
                cat_x = x
                y += 28

    if cat_x > x:
        y += 28
    y += 20

    # Enterprise list header
    draw.text((x, y), '【企业列表】', fill='#333333', font=font_header)
    y += 35

    # Build source index
    circled_nums = ['①', '②', '③', '④', '⑤', '⑥', '⑦', '⑧', '⑨', '⑩']
    source_list = []
    source_index = {}
    for e in enterprises:
        src = e.get('data_source', '').strip()
        if src and src not in source_index:
            idx = len(source_list)
            source_index[src] = circled_nums[idx] if idx < len(circled_nums) else f'[{idx+1}]'
            source_list.append(src)

    # Calculate columns: dynamically adjust entries per column based on total
    # count to keep legend height reasonable and balanced.
    if total_count <= 80:
        entries_per_col = 15
    elif total_count <= 120:
        entries_per_col = 20
    else:
        entries_per_col = 25
    num_cols = max(3, (total_count + entries_per_col - 1) // entries_per_col)
    col_width = (legend_width - 2 * margin) // num_cols
    col_gap = 10

    # Helper: wrap text to fit column width (max 2 lines, truncate with ...)
    def _wrap_label(text, font, max_w):
        """Split text into lines that fit within max_w."""
        # Measure average character width using a sample
        bbox = draw.textbbox((0, 0), "中文字", font=font)
        avg_char_w = (bbox[2] - bbox[0]) / 3
        max_chars = max(5, int(max_w / avg_char_w))
        if len(text) <= max_chars:
            return [text], 24
        # Try to find a good break point (prefer breaking at punctuation)
        break_at = max_chars
        for j in range(max_chars, max_chars // 2, -1):
            if text[j] in '、，,()（）':
                break_at = j + 1
                break
        first = text[:break_at]
        rest = text[break_at:]
        if len(rest) > max_chars:
            rest = rest[:max_chars - 1] + '…'
        return [first, rest], 44  # 2 lines need extra height

    # Render entries column by column
    col_heights = [0] * num_cols
    entries = list(enumerate(enterprises))
    label_max_w = col_width - 30  # leave padding for number + margin

    for col_idx in range(num_cols):
        col_x = margin + col_idx * col_width
        col_y = y
        start_idx = col_idx * entries_per_col
        end_idx = min(start_idx + entries_per_col, total_count)

        for i in range(start_idx, end_idx):
            e = enterprises[i]
            cat_keys = e['categories']
            num_color = cat_cfg.get(cat_keys[0], {}).get('color', '#CC0000')
            src_ref = source_index.get(e.get('data_source', '').strip(), '')

            label_lines, label_h = _wrap_label(e['label'], font_small, label_max_w)

            draw.text((col_x, col_y), f'{i + 1}', fill=num_color, font=font_small)
            draw.text((col_x + 26, col_y), label_lines[0], fill='#333333', font=font_small)
            col_y += 24

            for line in label_lines[1:]:
                draw.text((col_x + 26, col_y), line, fill='#555555', font=font_tiny)
                col_y += 20

            display_names = [cat_cfg.get(c, {}).get('display', c) for c in cat_keys]
            tag = '【' + '】【'.join(display_names) + '】'
            if src_ref:
                tag += f' {src_ref}'
            draw.text((col_x + 26, col_y), tag, fill='#888888', font=font_tiny)
            col_y += 24

        col_heights[col_idx] = col_y - y

    y += max(col_heights) + 20

    # Source type grouping
    source_type_labels = {
        'official': '官方监管名录',
        'license': '许可证信息',
        'complaint': '群众投诉/信访',
        'exposure': '违规曝光',
    }
    source_type_counts = {}
    for e in enterprises:
        st = e.get('source_type', '').strip()
        if st:
            source_type_counts[st] = source_type_counts.get(st, 0) + 1

    if source_type_counts:
        draw.text((x, y), '数据来源分布：', fill='#888888', font=font_tiny)
        y += 22
        st_x = x + 20
        for st, count in sorted(source_type_counts.items(), key=lambda x: -x[1]):
            label = source_type_labels.get(st, st)
            text = f'[{count}家] {label}'
            draw.text((st_x, y), text, fill='#888888', font=font_tiny)
            bbox = draw.textbbox((0, 0), text, font=font_tiny)
            st_x += (bbox[2] - bbox[0]) + 30
            if st_x > legend_width - 200:
                st_x = x + 20
                y += 20
        if st_x > x + 20:
            y += 20

    # Data source footer
    y += 15
    draw.text((x, y), '数据来源:', fill='#888888', font=font_tiny)
    y += 20
    for src in source_list:
        ref = source_index[src]
        draw.text((x, y), f'{ref} {src}', fill='#888888', font=font_tiny)
        y += 18

    # Conditional radiation footnote
    has_radiation = any('辐射安全' in e.get('categories', []) for e in enterprises)
    if has_radiation:
        y += 10
        draw.text((x, y), '注：辐射单位为射线装置使用单位', fill='#888888', font=font_tiny)
        y += 20

    # Crop to actual content
    actual_height = y + margin
    legend_img = legend_img.crop((0, 0, legend_width, actual_height))
    draw_final = ImageDraw.Draw(legend_img)
    draw_final.rectangle([(0, 0), (legend_width - 1, actual_height - 1)], outline='#cccccc', width=2)

    legend_path = os.path.join(os.path.dirname(config['meta']['output_path']) or '.', 'legend_only.png')
    ensure_dir(legend_path)
    legend_img.save(legend_path)
    print(f'Legend-only image saved: {legend_path} ({legend_width}x{actual_height})')
    return legend_img


def combine_images(map_path: str, legend_img: Image.Image, output_path: str, horizontal: bool = False) -> str:
    """Combine map and legend into final image.

    Default (vertical legend): map on left, legend on right, vertically centered.
    Horizontal mode (legend below map): map on top, legend below.
    """
    map_img = Image.open(map_path)
    legend_w, legend_h = legend_img.size

    if horizontal:
        total_width = max(map_img.width, legend_w)
        final_height = map_img.height + legend_h
        combined = Image.new('RGB', (total_width, final_height), 'white')
        # Center map horizontally
        map_x = (total_width - map_img.width) // 2
        combined.paste(map_img, (map_x, 0))
        # Legend at bottom, centered
        legend_x = (total_width - legend_w) // 2
        combined.paste(legend_img, (legend_x, map_img.height))
    else:
        final_height = max(map_img.height, legend_h)
        map_y = (final_height - map_img.height) // 2
        legend_y = (final_height - legend_h) // 2
        total_width = map_img.width + legend_w
        combined = Image.new('RGB', (total_width, final_height), 'white')
        combined.paste(map_img, (0, map_y))
        combined.paste(legend_img, (map_img.width, legend_y))

    ensure_dir(output_path)
    combined.save(output_path, quality=95)
    print(f'Final combined image saved: {output_path}')
    print(f'Image dimensions: {combined.size[0]} x {combined.size[1]} pixels')

    # Clean up intermediate files
    for f in [map_path, os.path.join(os.path.dirname(output_path) or '.', 'legend_only.png')]:
        if os.path.exists(f):
            os.remove(f)

    return output_path


def generate_map(config: dict, enterprises: list) -> str:
    """Orchestrate map generation: build data → render map → render legend → combine."""
    gdf_boundary_wm, gdf_wm = build_geodataframes(config, enterprises)
    map_path = render_basemap(gdf_boundary_wm, gdf_wm, config)

    # Determine layout mode based on enterprise count
    total_count = len(enterprises)
    horizontal_mode = total_count > 60

    # Get map width for horizontal legend sizing
    map_img = Image.open(map_path)
    map_width = map_img.width
    map_img.close()

    legend_img = render_legend(enterprises, config, map_width=map_width)
    return combine_images(map_path, legend_img, config['meta']['output_path'], horizontal=horizontal_mode)


def main():
    parser = argparse.ArgumentParser(description='Generate pollution-source distribution map')
    parser.add_argument('-c', '--config', required=True, help='Path to config YAML file')
    args = parser.parse_args()

    config = load_config(args.config)
    cache_file = config['gaode'].get('cache_file', './geocode_cache.json')
    enterprises = merge_coords(config['enterprises'], cache_file)

    if not enterprises:
        print('ERROR: no enterprises with valid coordinates. Run geocode.py first.')
        return 1

    # Normalize legacy single category to categories list
    _normalize_categories(enterprises)

    # Filter out enterprises with no valid coordinates
    original_count = len(enterprises)
    enterprises = [e for e in enterprises if e.get("lat") is not None and e.get("lon") is not None]
    no_coord_count = original_count - len(enterprises)
    if no_coord_count:
        print(f"\n⚠️  已过滤 {no_coord_count} 家无坐标企业（geocode 失败）")

    # Filter out cross-district enterprises (geocode_level == '跨区')
    # These are enterprises whose coordinates fall outside the target district.
    original_count = len(enterprises)
    enterprises = [e for e in enterprises if e.get("geocode_level") != "跨区"]
    filtered_count = original_count - len(enterprises)
    if filtered_count:
        print(f"\n⚠️  已过滤 {filtered_count} 家跨区企业（坐标不在目标行政区）")

    validate_data_sources(enterprises)
    lp, dup = validate_coords(enterprises)
    city = get_city_from_config(config)
    oob, out = validate_coord_reasonableness(enterprises, city)
    boundary_coords = config.get('boundary', {}).get('coords', [])
    outside = validate_coords_in_boundary(enterprises, boundary_coords)

    # Only block on severe errors (out-of-bounds = coordinate is wrong province/country).
    # Low precision, duplicates, and boundary violations are warned but not blocked,
    # because the user wants to fix real addresses proactively rather than block generation.
    severe_errors = oob + out
    if severe_errors > 0:
        print(f"\n{'='*50}")
        print(f"地图生成已中止：发现 {severe_errors} 家企业坐标严重异常（超出城市范围或极端偏离）")
        print(f"{'='*50}")
        print("请先修正上述问题后重新运行本脚本。")
        print("修正方式：")
        print("  1. 运行 python3 audit_coords.py --report -c config.yaml 查看详细报告")
        print("  2. 运行 python3 fix_addresses.py -c config.yaml --apply 自动修复地址")
        print("  3. 运行 python3 geocode.py -c config.yaml 重新编码坐标")
        return 1

    generate_map(config, enterprises)
    return 0


if __name__ == '__main__':
    exit(main())
