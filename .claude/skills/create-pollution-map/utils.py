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

try:
    from shapely.geometry import Polygon
except ImportError:
    Polygon = None


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
# City / district bounds (auto-inference)
# ---------------------------------------------------------------------------

# Lightweight city center lookup — only ~80 major cities.
# Boundaries are inferred as center ± 2° when no enterprise coords exist.
_CITY_CENTERS = {
    "福州": (26.08, 119.30), "厦门": (24.48, 118.09), "泉州": (24.91, 118.68),
    "漳州": (24.51, 117.65), "莆田": (25.45, 119.01), "龙岩": (25.08, 117.03),
    "三明": (26.27, 117.64), "南平": (27.29, 118.17), "宁德": (26.67, 119.55),
    "南京": (32.06, 118.78), "苏州": (31.30, 120.58), "无锡": (31.57, 120.30),
    "常州": (31.78, 119.95), "南通": (32.01, 120.86), "扬州": (32.39, 119.42),
    "徐州": (34.26, 117.28), "淮安": (33.60, 119.02), "盐城": (33.38, 120.15),
    "镇江": (32.19, 119.43), "泰州": (32.49, 119.92), "宿迁": (33.96, 118.28),
    "杭州": (30.27, 120.15), "宁波": (29.87, 121.55), "温州": (28.00, 120.70),
    "嘉兴": (30.75, 120.75), "湖州": (30.86, 120.10), "绍兴": (30.00, 120.58),
    "金华": (29.08, 119.65), "衢州": (28.93, 118.87), "舟山": (30.00, 122.10),
    "台州": (28.66, 121.42), "丽水": (28.45, 119.92),
    "广州": (23.13, 113.26), "深圳": (22.54, 114.06), "东莞": (23.05, 113.75),
    "佛山": (23.02, 113.12), "中山": (22.52, 113.39), "珠海": (22.27, 113.57),
    "惠州": (23.08, 114.42), "江门": (22.58, 113.08), "肇庆": (23.05, 112.47),
    "汕头": (23.35, 116.71), "潮州": (23.66, 116.63), "揭阳": (23.55, 116.37),
    "湛江": (21.27, 110.36), "茂名": (21.66, 110.92), "阳江": (21.86, 111.98),
    "清远": (23.68, 113.06), "韶关": (24.81, 113.60), "梅州": (24.29, 116.12),
    "河源": (23.74, 114.70), "汕尾": (22.79, 115.37), "云浮": (22.92, 112.04),
    "成都": (30.57, 104.07), "绵阳": (31.47, 104.68), "德阳": (31.13, 104.40),
    "重庆": (29.56, 106.55), "武汉": (30.59, 114.31), "长沙": (28.23, 112.98),
    "郑州": (34.75, 113.63), "西安": (34.34, 108.94), "沈阳": (41.80, 123.43),
    "大连": (38.91, 121.62), "长春": (43.82, 125.32), "哈尔滨": (45.80, 126.53),
    "济南": (36.65, 117.00), "青岛": (36.07, 120.38), "烟台": (37.46, 121.45),
    "天津": (39.13, 117.20), "北京": (39.90, 116.41), "上海": (31.23, 121.47),
    "石家庄": (38.04, 114.51), "太原": (37.87, 112.55), "合肥": (31.82, 117.23),
    "南昌": (28.68, 115.89), "昆明": (25.04, 102.71), "贵阳": (26.65, 106.63),
    "南宁": (22.82, 108.32), "兰州": (36.06, 103.83), "海口": (20.02, 110.35),
    "乌鲁木齐": (43.83, 87.62), "拉萨": (29.65, 91.12), "呼和浩特": (40.84, 111.75),
    "银川": (38.49, 106.23), "西宁": (36.62, 101.78), "三亚": (18.25, 109.51),
}


def estimate_city_bounds(city: str, enterprises: list = None) -> tuple:
    """Estimate reasonable coordinate bounds for a city.

    Strategy:
      1. If enterprises with lat/lon exist, use median ± 2°.
      2. Fallback to _CITY_CENTERS lookup (center ± 2°).
      3. Ultimate fallback: China-wide bounds.

    Returns (lat_min, lat_max, lon_min, lon_max).
    """
    if enterprises:
        lats = [e['lat'] for e in enterprises if 'lat' in e and e['lat'] is not None]
        lons = [e['lon'] for e in enterprises if 'lon' in e and e['lon'] is not None]
        if len(lats) >= 3:
            lats_sorted = sorted(lats)
            lons_sorted = sorted(lons)
            median_lat = lats_sorted[len(lats_sorted) // 2]
            median_lon = lons_sorted[len(lons_sorted) // 2]
            return (median_lat - 2.0, median_lat + 2.0, median_lon - 2.0, median_lon + 2.0)

    center = _CITY_CENTERS.get(city)
    if center:
        lat_c, lon_c = center
        return (lat_c - 2.0, lat_c + 2.0, lon_c - 2.0, lon_c + 2.0)

    # Ultimate fallback: China-wide
    return (18.0, 54.0, 73.0, 135.0)


def get_district_bounds(city: str, district: str, enterprises: list = None) -> "Polygon | None":
    """Estimate a district boundary from enterprise coordinates or city center.

    Strategy:
      1. If enterprises with coords exist for this district, use their bbox + 10% padding.
      2. Fallback: city center ± 0.18° (~20 km).

    Returns shapely Polygon or None.
    """
    if Polygon is None:
        return None

    if enterprises:
        coords = []
        for e in enterprises:
            d = e.get("district", "") if isinstance(e, dict) else getattr(e, "district", "")
            name = e.get("name", "") if isinstance(e, dict) else getattr(e, "name", "")
            lat = e.get("lat") if isinstance(e, dict) else getattr(e, "lat", None)
            lon = e.get("lon") if isinstance(e, dict) else getattr(e, "lon", None)
            if district in d or district in name:
                if lat is not None and lon is not None:
                    coords.append((lon, lat))
        if len(coords) >= 3:
            lons = [c[0] for c in coords]
            lats = [c[1] for c in coords]
            lon_pad = max((max(lons) - min(lons)) * 0.1, 0.02)
            lat_pad = max((max(lats) - min(lats)) * 0.1, 0.02)
            return Polygon([
                (min(lons) - lon_pad, min(lats) - lat_pad),
                (max(lons) + lon_pad, min(lats) - lat_pad),
                (max(lons) + lon_pad, max(lats) + lat_pad),
                (min(lons) - lon_pad, max(lats) + lat_pad),
            ])

    # Fallback: city center + rough radius
    center = _CITY_CENTERS.get(city)
    if center:
        lat_c, lon_c = center
        r = 0.18  # ~20 km
        return Polygon([
            (lon_c - r, lat_c - r), (lon_c + r, lat_c - r),
            (lon_c + r, lat_c + r), (lon_c - r, lat_c + r),
        ])
    return None


def coord_in_bounds(lat: float, lon: float, bounds: tuple) -> bool:
    """Check if a coordinate is within bounds (lat_min, lat_max, lon_min, lon_max)."""
    return bounds[0] <= lat <= bounds[1] and bounds[2] <= lon <= bounds[3]


def infer_address_from_name(name: str, city: str, district: str) -> str:
    """Generate a candidate address from enterprise name when address is empty.

    Rules (in priority order):
      1. Public utilities (污水处理厂/净水厂) → "{city}{district}{name}"
      2. Name contains explicit road/street → extract it
      3. Branch company (分公司/分厂) → use parent company name
      4. POI-friendly types (医院/学校/研究所) → "{city}{district}{name}"
      5. Default → "{city}{district}{name}"
    """
    if not name:
        return ""

    # Rule 1: public utilities — always construct full address
    utility_keywords = ["污水处理厂", "污水厂", "水处理厂", "净水厂", "自来水厂",
                        "垃圾处理厂", "垃圾焚烧厂", "固废处置", "危废处置"]
    if any(kw in name for kw in utility_keywords):
        return f"{city}{district}{name}"

    # Rule 2: explicit road/street in name
    road_match = re.search(r'([^\s,，]{2,8}(?:路|街|道|大道|巷|胡同)[^\s,，]*(?:\d+号?)?)', name)
    if road_match:
        return f"{city}{district}{road_match.group(1)}"

    # Rule 3: branch company — try parent name
    if "分公司" in name or "分厂" in name:
        parent = name.replace("分公司", "").replace("分厂", "").strip()
        if len(parent) >= 4:
            return f"{city}{district}{parent}"

    # Rule 4: POI-friendly types (hospitals, schools, research institutes)
    poi_friendly = ["医院", "大学", "学院", "中学", "小学", "研究所", "研究院"]
    if any(kw in name for kw in poi_friendly):
        return f"{city}{district}{name}"

    # Rule 5: default
    return f"{city}{district}{name}"


# Production-address variant query rules.
# When an enterprise may have separate office vs production addresses,
# these rules generate alternative POI search queries that target
# the production / facility location rather than the headquarters.
_PRODUCTION_QUERY_RULES = {
    "factory": {
        "keywords": ["制药", "化工", "制造", "机械", "电子", "纺织", "食品", "建材",
                     "钢铁", "冶金", "造纸", "印染", "鞋业", "陶瓷", "玻璃", "塑胶",
                     "印刷", "涂装", "电镀", "皮革", "橡塑", "化纤", "水泥", "砖瓦",
                     "装备", "航空", "船舶", "汽车", "电机", "汽轮", "重工", "轻工",
                     "材料", "金属", "矿业", "采选", "冶炼", "轧制", "铸造", "锻造",
                     "碳业", "纤维", "复合材料", "新能源", "新材料"],
        "suffixes": ["厂区", "生产基地", "工厂", "产业园", "工业园", "工业区", "制造基地"],
    },
    "hospital": {
        "keywords": ["医院", "卫生院", "防治院", "疗养院", "疾控中心"],
        "suffixes": ["院区", "分院", "新院区", "南院区", "北院区", "东院区", "西院区"],
    },
    "university": {
        "keywords": ["大学", "学院", "研究所", "研究院"],
        "suffixes": ["校区", "实验中心", "实验室", "中试基地", "科创园", "科技园"],
    },
    "sewage": {
        "keywords": ["污水处理", "污水厂", "水处理", "净水厂", "自来水厂", "排水"],
        "suffixes": ["处理厂", "厂", "处理站", "净水厂", "净化厂"],
    },
    "power": {
        "keywords": ["发电", "热电", "能源", "电厂", "电站", "光伏", "风电", "水电"],
        "suffixes": ["发电厂", "电厂", "电站", "能源基地", "风电场", "光伏基地"],
    },
    "slaughter": {
        "keywords": ["屠宰"],
        "suffixes": ["屠宰场", "屠宰厂", "加工厂"],
    },
    "livestock": {
        "keywords": ["养殖", "畜牧", "猪场", "鸡场", "奶牛", "肉牛", "家禽"],
        "suffixes": ["养殖场", "养殖基地", "牧场", "养殖小区"],
    },
}


def build_production_queries(name: str, city: str, district: str, max_variants: int = 3) -> list:
    """Generate production-address variant queries for POI search.

    When an enterprise has separate office and production addresses,
    searching for "XX公司厂区" or "XX生产基地" often yields the actual
    pollution-source location instead of the headquarters office.

    Returns a list of variant query strings (may be empty).
    """
    if not name:
        return []

    # Strip common corporate suffixes to get the core name
    core = name
    for suffix in ["有限公司", "有限责任公司", "股份有限公司", "股份公司", "公司", "集团", "总厂", "厂"]:
        if core.endswith(suffix):
            core = core[: -len(suffix)].strip()
            break

    # Detect enterprise type
    ent_type = None
    for type_key, cfg in _PRODUCTION_QUERY_RULES.items():
        if any(kw in name for kw in cfg["keywords"]):
            ent_type = type_key
            break

    if not ent_type:
        return []

    def _make_variant(base, suffix):
        """Combine base + suffix, skipping if suffix would create duplication."""
        if not base:
            return None
        # Skip bare "厂" after corporate names — produces useless queries like "XX公司厂"
        if suffix == "厂" and (base.endswith("公司") or base.endswith("有限") or base.endswith("集团")):
            return None
        if suffix in base:
            return f"{base} {city}{district}"
        combined = base + suffix
        # Check for consecutive duplicate 2-grams (e.g. "处理处理", "厂厂")
        for i in range(len(combined) - 3):
            if combined[i:i+2] == combined[i+2:i+4]:
                return f"{base} {city}{district}"
        return f"{base}{suffix} {city}{district}"

    variants = []
    suffixes = _PRODUCTION_QUERY_RULES[ent_type]["suffixes"]
    for suffix in suffixes[:max_variants]:
        # Core name + suffix + location
        if core:
            v = _make_variant(core, suffix)
            if v:
                variants.append(v)
        # Full name + suffix + location
        v = _make_variant(name, suffix)
        if v:
            variants.append(v)

    # Deduplicate
    seen = set()
    unique = []
    for q in variants:
        if q not in seen and len(q) >= 6:
            seen.add(q)
            unique.append(q)
    return unique


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


def extract_target_district(config: dict, filename_hint: str = "") -> str:
    """Extract target district from config title, explicit fields, or enterprise names.

    Priority order:
      1. config['target_district']
      2. config['meta']['district']
      3. Title (e.g. "厦门市集美区2025年..." → "集美区")
      4. Filename hint (e.g. "南京_鼓楼区_2025.yaml" → "鼓楼区")
      5. Enterprise records' district field (if all same)
      6. First enterprise name
    """
    # 1. Explicit config
    td = config.get("target_district", "").strip()
    if td:
        return td

    # 2. From meta.district
    meta = config.get("meta", {})
    td = meta.get("district", "").strip()
    if td:
        return td

    # 3. From meta title, e.g. "厦门市集美区2025年..." → "集美区"
    title = meta.get("title", "")
    for kw in _DISTRICT_KEYWORDS:
        if kw in title:
            return kw

    # 4. From filename hint
    if filename_hint:
        for kw in _DISTRICT_KEYWORDS:
            if kw in filename_hint:
                return kw

    # 5. From enterprises' district field (only if unanimous)
    enterprises = config.get("enterprises", [])
    districts = set()
    for ent in enterprises:
        d = ent.get("district", "") if isinstance(ent, dict) else getattr(ent, "district", "")
        if d:
            districts.add(d)
    if len(districts) == 1:
        return districts.pop()

    # 6. From first enterprise name
    if enterprises:
        hint = extract_district_hint(enterprises[0].get("name", "") if isinstance(enterprises[0], dict) else getattr(enterprises[0], "name", ""))
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
