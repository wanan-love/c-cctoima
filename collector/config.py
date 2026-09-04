"""全局配置：入口 URL、运营商元数据、河北标识、路径常量。

所有"官方入口 URL"均来自任务指定；分类/分页参数一律运行时从页面发现，
本文件不得包含任何资费分类列表。
"""
from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
FIXTURES_DIR = PROJECT_ROOT / "fixtures"

PROVINCE = "河北"
PROVINCE_FULL = "河北省"
AUDIENCE = "个人"

# ── 官方资费专区入口（任务指定，采集器从这里进入并按页面交互选河北+个人） ──
ENTRY_URLS = {
    "cmcc": "https://h.app.coc.10086.cn/cmcc-app/pc-pages/tariffZonePers.html?pageId=834148205904408576&prov=311&channelId=P00000010686",
    "cucc": "https://img.client.10010.com/zifeizhuanquwt/index.html#/",
    "ctcc": "https://www.189.cn/tariffZone/",
    "cbn": "https://m.10099.com.cn/expensesNotice/#/home",
}

OPERATOR_META = {
    "cmcc": {"name": "中国移动", "cn_short": "移动", "ima_folder": "河北移动", "source_name": "中国移动河北公司资费公示专区"},
    "cucc": {"name": "中国联通", "cn_short": "联通", "ima_folder": "河北联通", "source_name": "中国联通资费专区"},
    "ctcc": {"name": "中国电信", "cn_short": "电信", "ima_folder": "河北电信", "source_name": "中国电信网厅资费专区"},
    "cbn": {"name": "中国广电", "cn_short": "广电", "ima_folder": "河北广电", "source_name": "中国广电资费公示"},
}

OPERATORS = list(OPERATOR_META.keys())

# ── 河北标识（用于完整性校验的证据锚点；由实测页面/接口发现） ──
HEBEI_EVIDENCE = {
    "cmcc": {
        "prov_entry_text": "河北省",       # 页面 .prov-entry 展示文本
        "api_province": "311",             # getTariffListInfo 明文中的 province 字段
        "report_prefix": ("HE", "JT"),     # reportNo 前缀（HE=河北上报，JT=集团公共）
        "audience_tab": "个人资费",         # 页面上的受众页签
        "range_tab": "河北资费",            # 页面上的范围页签（分省）
    },
    "cucc": {
        "prov_code": "018",
        "city_code": "188",                # 石家庄（省会，页面级联第二级）
        "report_prefix": "HE",
        "tariff_attributes": "2",          # 本省资费
    },
    "ctcc": {
        "prov_code": "609906",             # 前端配置中的河北 provinceCode
        "city_code": "he",
    },
    "cbn": {
        "area_code": "HB00",               # qryAreaList 中河北 areaCode
        "type_public": "GZ",               # queryTariffCondition 中"公众"(=个人) type1
    },
}

USER_AGENTS = {
    "desktop": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "mobile": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
}

# ── IMA ──
IMA_BASE_URL = "https://ima.qq.com"
IMA_KB_NAME = "河北运营商资费"

# ── 完整性阈值 ──
INTEGRITY = {
    # 数量下降保护：相对上次成功版本，下降比例与绝对值同时超阈值才判可疑
    "drop_ratio_threshold": 0.30,   # 30%
    "drop_abs_threshold": 20,       # 且绝对减少 > 20 条
    "detail_coverage_min": 0.95,    # 详情字段覆盖率下限
    "duplicate_max": 0,             # 允许的重复 tariff_id 上限
    "api_error_max": 0,             # 允许的严重 API 错误上限
}

# ── 采集节奏（真人模式，全部随机抖动） ──
PACING = {
    "nav_timeout_ms": 60000,
    "short_jitter": (0.8, 2.0),
    "mid_jitter": (2.0, 5.0),
    "long_jitter": (8.0, 14.0),
    "api_interval": (0.6, 1.8),     # 页面内批量接口调用间隔
    "scroll_interval": (1.6, 3.0),
    "type_switch": (8.0, 16.0),
    "retry_cooldown": (45.0, 90.0),
}


def data_dir_for(op: str) -> Path:
    return DATA_DIR / op


def ensure_dirs(op: str) -> Path:
    d = data_dir_for(op)
    (d / "raw").mkdir(parents=True, exist_ok=True)
    d.mkdir(parents=True, exist_ok=True)
    return d
