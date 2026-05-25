#!/usr/bin/env python3
"""Shared utilities for pollution-source map generation."""

import math
import os
import re
import urllib.parse
import difflib
import requests
import yaml
from PIL import ImageFont


def get_gaode_key(config_key: str = "") -> str:
    """Return Gaode API key: env var GAODE_API_KEY takes priority, else config value."""
    return os.environ.get("GAODE_API_KEY", config_key)


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base. Lists and scalars are replaced, not appended."""
    result = dict(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


def load_config_with_defaults(config_path: str) -> dict:
    """Load a city YAML and merge with defaults.yaml.

    City YAML values override defaults. Defaults are looked up next to this file.
    """
    with open(config_path, "r", encoding="utf-8") as f:
        user_cfg = yaml.safe_load(f)

    defaults_path = os.path.join(os.path.dirname(__file__), "defaults.yaml")
    if os.path.exists(defaults_path):
        with open(defaults_path, "r", encoding="utf-8") as f:
            defaults = yaml.safe_load(f)
        return _deep_merge(defaults, user_cfg)
    return user_cfg


# ---------------------------------------------------------------------------
# Name matching & district extraction
# ---------------------------------------------------------------------------

_DISTRICT_KEYWORDS = [
    # Fuzhou
    "长乐区", "长乐市", "长乐", "晋安区", "仓山区", "鼓楼区", "台江区", "马尾区",
    "闽侯县", "闽侯", "连江县", "罗源县", "闽清县", "永泰县", "福清市",
    "金峰镇", "松下镇", "江田镇", "文武砂街道", "湖南镇", "航城街道",
    "营前街道", "漳港街道", "梅花镇", "潭头镇", "玉田镇", "鹤上镇",
    "古槐镇", "文岭镇", "猴屿乡", "首祉村", "石壁村", "连坂村",
    "白沙镇", "洋里乡", "大湖乡", "尚干镇", "青口镇", "南屿镇",
    # Quanzhou
    "鲤城区", "丰泽区", "洛江区", "泉港区",
    "晋江市", "晋江", "石狮市", "石狮", "南安市", "南安",
    "惠安县", "惠安", "安溪县", "安溪", "永春县", "永春", "德化县", "德化",
    # Xiamen
    "思明区", "湖里区", "集美区", "海沧区", "同安区", "翔安区",
    # Zhangzhou
    "芗城区", "龙文区", "龙海区", "长泰区",
    # Putian
    "城厢区", "涵江区", "荔城区", "秀屿区", "仙游县",
    # Longyan
    "新罗区", "永定区", "漳平市",
    # Sanming
    "三元区", "沙县区", "永安市",
    # Nanping
    "延平区", "建阳区", "建瓯市", "武夷山市", "邵武市",
    # Ningde
    "蕉城区", "福安市", "福鼎市",
]


def extract_district_hint(name: str):
    """Extract district/township keyword from enterprise name."""
    for kw in _DISTRICT_KEYWORDS:
        if kw in name:
            return kw
    return None


def extract_target_district(config: dict) -> str:
    """Extract target district from config title or enterprise names.

    Tries config['target_district'] first, then title, then enterprise names.
    """
    # 1. Explicit config
    td = config.get("target_district", "").strip()
    if td:
        return td

    # 2. From meta title, e.g. "厦门市集美区2025年..." → "集美区"
    title = config.get("meta", {}).get("title", "")
    for kw in _DISTRICT_KEYWORDS:
        if kw in title:
            return kw

    # 3. From first enterprise name
    enterprises = config.get("enterprises", [])
    if enterprises:
        hint = extract_district_hint(enterprises[0].get("name", ""))
        if hint:
            return hint

    return ""


def district_match(name_hint: str, addr_district: str) -> bool:
    """Check if name district hint matches address district.

    Handles aliases like 长乐市 vs 长乐区.
    """
    if not name_hint or not addr_district:
        return True
    nh = name_hint.replace("市", "").replace("区", "").replace("县", "")
    ad = addr_district.replace("市", "").replace("区", "").replace("县", "")
    return nh in ad or ad in nh


# ---------------------------------------------------------------------------
# Address quality
# ---------------------------------------------------------------------------

_ADDRESS_SPECIFIC_PATTERNS = re.compile(
    r'[路街道巷号村镇乡大道]'
    r'|工业园区|工业区|科技园|开发区|高新区|保税区|物流园'
    r'|北[一二三四五六七八九十]*路|南[一二三四五六七八九十]*路'
    r'|东[一二三四五六七八九十]*路|西[一二三四五六七八九十]*路'
    r'|[东南西北][一二三四五六七八九十]*街'
)


def is_pseudo_address(address: str, name: str) -> bool:
    """Check if address is just a placeholder without real street/number.

    A pseudo address looks like: '厦门市集美区厦门成联五金制造有限公司'
    which contains no actual door number, street name, or industrial park.
    A real address looks like: '厦门市集美区杏前路203号' or '灌口镇金龙路699号'.
    """
    if not address or not name:
        return True

    # Remove the company name itself from the address
    cleaned = address.replace(name, '').strip()

    # After removing the name, if nothing useful remains
    if not cleaned or len(cleaned) < 3:
        return True

    # Check for specific address elements (street, number, village, town, etc.)
    has_specific = bool(_ADDRESS_SPECIFIC_PATTERNS.search(cleaned))
    return not has_specific


def address_quality_score(address: str, name: str) -> dict:
    """Score address quality on multiple dimensions.

    Returns a dict:
      {
        'is_pseudo': bool,
        'has_street': bool,      # 路/街/道
        'has_number': bool,      # 号/栋/层
        'has_district': bool,    # 区/县/镇
        'has_park': bool,        # 工业园区/科技园
        'score': int,            # 0-100, higher is better
        'level': str,            # 'excellent'|'good'|'fair'|'poor'|'pseudo'
      }
    """
    result = {
        'is_pseudo': False,
        'has_street': False,
        'has_number': False,
        'has_district': False,
        'has_park': False,
        'score': 0,
        'level': 'pseudo',
    }

    if not address:
        result['is_pseudo'] = True
        return result

    # Check if this is a pseudo address (no street/number/park info)
    if is_pseudo_address(address, name):
        result['is_pseudo'] = True
        return result

    cleaned = address.replace(name, '').strip()

    # Check components
    result['has_street'] = bool(re.search(r'[路街道道巷]', cleaned))
    result['has_number'] = bool(re.search(r'[号栋层单元室]', cleaned))
    result['has_district'] = bool(re.search(r'[区县镇乡]', cleaned))
    result['has_park'] = bool(re.search(r'工业园区|工业区|科技园|开发区|高新区|保税区', cleaned))

    # Score
    score = 0
    if result['has_district']:
        score += 20
    if result['has_street']:
        score += 30
    if result['has_number']:
        score += 30
    if result['has_park']:
        score += 20
    result['score'] = score

    # Level
    if score >= 80:
        result['level'] = 'excellent'
    elif score >= 60:
        result['level'] = 'good'
    elif score >= 40:
        result['level'] = 'fair'
    elif score >= 20:
        result['level'] = 'poor'
    else:
        result['is_pseudo'] = True

    return result


def _normalize_name(name: str) -> str:
    """Strip administrative prefixes and company suffixes to extract core name."""
    n = name
    # Remove bracketed content
    n = re.sub(r"[（(].*?[）)]", "", n)
    # Strip common administrative prefixes
    for prefix in ["福建省", "福建", "福州市", "福州", "长乐市", "长乐区", "长乐", "闽侯县", "闽侯",
                   "鼓楼区", "台江区", "仓山区", "晋安区", "马尾区", "连江县", "罗源县",
                   "闽清县", "永泰县", "福清市", "高新区"]:
        n = n.replace(prefix, "")
    # Strip common suffixes / type words
    for suffix in ["有限公司", "有限责任公司", "公司", "厂", "分公司", "分厂",
                   "停车场(出入口)", "党支部", "中共"]:
        n = n.replace(suffix, "")
    return n.strip()


def _names_match(a: str, b: str) -> bool:
    """Strict name validation: reject approximate / same-name matches.

    Only passes if:
      1. Exact match, OR
      2. One name contains the other (after stripping common suffixes), OR
      3. Normalized core names match exactly.

    The old 70% fuzzy threshold (difflib.SequenceMatcher) is REMOVED
    because it accepted too many wrong POI hits (e.g. different ABB
    subsidiaries or same-industry companies with similar names).
    """
    # 1. Exact match
    if a == b:
        return True

    # 2. Direct substring (one contains the other)
    # Require at least 4 characters to avoid trivial matches
    if len(a) >= 4 and len(b) >= 4 and (a in b or b in a):
        return True

    # 3. Normalize and exact-match the core name
    na, nb = _normalize_name(a), _normalize_name(b)
    if not na or not nb:
        return False
    if na == nb:
        return True

    # 4. Normalized substring with minimum length guard
    min_core = 4
    if len(na) >= min_core and len(nb) >= min_core and (na in nb or nb in na):
        return True

    return False


# ---------------------------------------------------------------------------
# Geocoding & reverse geocoding
# ---------------------------------------------------------------------------

def geocode_address(address: str, key: str, city: str = "", timeout: int = 10):
    """Geocode a single address via Gaode Maps API.

    Args:
        city: Restrict geocoding to this city (e.g. "福州", "泉州").
              Prevents generic addresses like "园南路" from matching other provinces.

    Returns (lon, lat, level) or (None, None, None) on failure.
    """
    encoded = urllib.parse.quote(address)
    url = f"https://restapi.amap.com/v3/geocode/geo?address={encoded}&key={key}"
    if city:
        url += f"&city={urllib.parse.quote(city)}"
    try:
        resp = requests.get(url, timeout=timeout).json()
        if resp.get("status") == "1" and resp.get("geocodes"):
            loc = resp["geocodes"][0]["location"]
            lon, lat = loc.split(",")
            level = resp["geocodes"][0].get("level", "未知")
            return float(lon), float(lat), level
    except Exception:
        pass
    return None, None, None


def reverse_geocode_district(lat: float, lon: float, key: str, timeout: int = 10) -> str:
    """Reverse geocode to get the district name for a coordinate.

    Returns district name like '闽侯县' or empty string on failure.
    """
    url = (
        f"https://restapi.amap.com/v3/geocode/regeo"
        f"?location={lon},{lat}&key={key}&extensions=all"
    )
    try:
        resp = requests.get(url, timeout=timeout).json()
        if resp.get("status") == "1" and resp.get("regeocode"):
            addr_comp = resp["regeocode"].get("addressComponent", {})
            return addr_comp.get("district", "")
    except Exception:
        pass
    return ""


def fetch_boundary_datav(adcode: str, timeout: int = 30):
    """Fetch district boundary from DataV GeoJSON API.

    Returns list of [lon, lat] coordinate pairs for the largest polygon,
    or empty list on failure.
    """
    url = f"https://geo.datav.aliyun.com/areas_v3/bound/{adcode}.json"
    try:
        resp = requests.get(url, timeout=timeout)
        data = resp.json()
        features = data.get("features", [])
        if not features:
            return []
        coords = features[0]["geometry"]["coordinates"]
        largest = None
        max_len = 0
        for multipoly in coords:
            for poly in multipoly:
                if len(poly) > max_len:
                    max_len = len(poly)
                    largest = poly
        if largest:
            return largest[:-1] if largest[0] == largest[-1] else largest
    except Exception:
        pass
    return []


# ---------------------------------------------------------------------------
# Font & YAML utilities
# ---------------------------------------------------------------------------

def get_font(size: int) -> ImageFont.FreeTypeFont:
    """Load a CJK-capable font, macOS and Linux."""
    font_paths = [
        # macOS
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/PingFang SC.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/System/Library/Fonts/Arial Unicode.ttf",
        "/Library/Fonts/Arial Unicode.ttf",
        # Linux (Ubuntu/Debian)
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    ]
    for path in font_paths:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def write_yaml_config(data: dict, output_path: str):
    """Write a config dict to a YAML file with Chinese-friendly formatting."""
    with open(output_path, "w", encoding="utf-8") as f:
        yaml.dump(
            data, f,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
            width=200,
        )


def get_city_from_config(config: dict) -> str:
    """Extract city name from config (gaode.city), default '福州'."""
    return config.get("gaode", {}).get("city", "福州")


def _is_in_data_dir(config_dir: str, city: str) -> bool:
    """Check if config is already inside data/<city>/ or outputs/<city>/."""
    norm = config_dir.replace("\\", "/")
    return f"outputs/{city}" in norm or f"data/{city}" in norm


def _normalize_rel_path(filepath: str) -> str:
    """Strip leading ./ or ../ from a relative path."""
    clean = filepath
    while clean.startswith("./") or clean.startswith("../"):
        clean = clean.lstrip("./").lstrip("../")
    return clean


def resolve_image_path(config: dict, config_dir: str, filepath: str) -> str:
    """Resolve image output path to data/<city>/images/.

    Rules:
      - Absolute paths → returned as-is.
      - Config in root → data/<city>/images/<filename>.
      - Config already in data/<city>/ → <config_dir>/images/<filename>.
    """
    if not filepath or os.path.isabs(filepath):
        return filepath
    clean = _normalize_rel_path(filepath)
    city = get_city_from_config(config)
    if _is_in_data_dir(config_dir, city):
        return os.path.join(config_dir, "images", clean)
    return os.path.join(config_dir, "data", city, "images", clean)


def resolve_cache_path(config: dict, config_dir: str, filepath: str) -> str:
    """Resolve cache path to cache/ or data/<city>/.

    Rules:
      - Absolute paths → returned as-is.
      - Config in root → cache/<filename> (centralised cache dir).
      - Config already in data/<city>/ → <config_dir>/<filename>.
    """
    if not filepath or os.path.isabs(filepath):
        return filepath
    clean = _normalize_rel_path(filepath)
    city = get_city_from_config(config)
    if _is_in_data_dir(config_dir, city):
        return os.path.join(config_dir, clean)
    return os.path.join(config_dir, "cache", clean)


def ensure_dir(path: str):
    """Ensure the directory for a file path exists."""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
