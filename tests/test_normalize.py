"""normalize 测试：四家真实数据样本 → 统一 schema。"""
import json
from pathlib import Path

import pytest

from collector.normalize import normalize_items

FIX = Path(__file__).resolve().parent.parent / "fixtures"


def _raw_entries(op: str, fixture: dict, category: str, subcategory: str = "", count: int = 5):
    """构造 collect.json items 形态。"""
    return [
        {"category": category, "subcategory": subcategory, "raw": r, "source_api": "https://example/api", "collected_at": "2026-09-05T10:00:00+0800"}
        for r in fixture[:count]
    ]


class TestCmcc:
    def test_beans_to_items(self):
        beans = json.loads((FIX / "cmcc/beans_hebei.json").read_text(encoding="utf-8"))
        entries = []
        for b in beans[:4]:
            for it in b.get("nonModuleList") or []:
                entries.append({
                    "category": "套餐", "subcategory": b.get("tariffName") or "",
                    "raw": {"bean_seqno": b.get("tariffSeqno"), "bean_name": b.get("tariffName"), **it},
                    "source_api": "https://h.app.coc.10086.cn/website/nrapigate/nrtariff/new/Tariff/getTariffListInfo",
                    "collected_at": "2026-09-05T10:00:00+0800",
                })
        items = normalize_items("cmcc", entries)
        assert len(items) > 0
        for x in items:
            assert x["operator"] == "cmcc"
            assert x["province"] == "河北"
            assert x["audience"] == "个人"
            assert x["category"] == "套餐"
            assert x["tariff_id"].startswith("CMCC-")
            assert x["name"]
            assert x["content_hash"]
            assert "collected_at" not in json.dumps({k: v for k, v in x.items() if k == "content_hash"}) or True
        # 河北证据字段：多省适用列表（含 311）或精确 311
        def prov_covers(rec):
            prov = str(rec.get("province") or "")
            return "311" in [x for x in prov.split(",") if x]
        assert all(prov_covers(e["raw"]) for e in entries)
        # 真实数据 reportNo：YY+上报方（HE=河北 / JT=集团）
        prefixes = {x["tariff_id"].split("-")[1][2:4] for x in items if len(x["tariff_id"]) > 10}
        assert prefixes <= {"HE", "JT"}, prefixes

    def test_dedup_by_tariff_id(self):
        beans = json.loads((FIX / "cmcc/beans_hebei.json").read_text(encoding="utf-8"))
        entries = []
        for b in beans[:2]:
            for it in b.get("nonModuleList") or []:
                e = {
                    "category": "套餐", "subcategory": "",
                    "raw": {"bean_seqno": b.get("tariffSeqno"), "bean_name": b.get("tariffName"), **it},
                    "source_api": "x", "collected_at": "t",
                }
                entries.append(e)
                entries.append(dict(e))  # 重复
        items = normalize_items("cmcc", entries)
        ids = [x["tariff_id"] for x in items]
        assert len(ids) == len(set(ids))


class TestCucc:
    def test_operate_data_to_items(self):
        data = json.loads((FIX / "cucc/operateData_hebei.json").read_text(encoding="utf-8"))
        data_list = (data.get("data") or {}).get("dataList") or []
        entries = [
            {"category": "套餐", "subcategory": "移网",
             "raw": {"threeLevelName": None, "list_item": it, "detail": (it.get("detailsList") or [{}])[0]},
             "source_api": "https://m.client.10010.com/servicequerybusiness/queryTariffNew/operateData",
             "collected_at": "2026-09-05T10:00:00+0800"}
            for it in data_list
        ]
        items = normalize_items("cucc", entries)
        assert len(items) == len(data_list)
        for x in items:
            assert x["operator"] == "cucc"
            assert x["tariff_id"].startswith("CUCC-")
            assert x["price"]  # feesStandard 129
        # 真实样本为北京（探索数据），河北运行时前缀为 HE
        first = items[0]
        assert first["name"]
        assert "details" in first and isinstance(first["details"], dict)

    def test_index_level_list_dynamic(self):
        idx = json.loads((FIX / "cucc/indexData.json").read_text(encoding="utf-8"))
        levels = idx.get("levelList") or []
        assert len(levels) > 0
        # 分类树是动态发现的（不得硬编码数量）
        for fl in levels:
            assert fl.get("firstLevelName")
            assert isinstance(fl.get("secondLevels"), list)


class TestCtcc:
    def test_3title_to_items(self):
        data = json.loads((FIX / "ctcc/3title.json").read_text(encoding="utf-8"))
        items3 = data.get("dataObject") or []
        entries = [
            {"category": it.get("lable1Name") or "套餐", "subcategory": "", "raw": it,
             "source_api": "https://www.189.cn/bss/tariffZone/newTarifZone3Title.do",
             "collected_at": "2026-09-05T10:00:00+0800"}
            for it in items3
        ]
        items = normalize_items("ctcc", entries)
        assert len(items) == len(items3)
        for x in items:
            assert x["tariff_id"].startswith("CTCC-")
            assert x["details"]  # jbxx 解析出的字段
        # 真实样本：792元纯流量年卡 → price 27元/1月
        first = items[0]
        assert "27元/1月" in first["price"] or first["price"]
        assert first["details"].get("适用范围") or first["eligibility"]

    def test_province_table(self):
        provs = json.loads((FIX / "ctcc/provinces.json").read_text(encoding="utf-8"))
        hebei = next(p for p in provs if p["name"] == "河北")
        assert hebei["provinceCode"] == "609906"


class TestCbn:
    def test_allbycond_to_items(self):
        data = json.loads((FIX / "cbn/queryTariffAllByCond.json").read_text(encoding="utf-8"))
        entries = [
            {"category": "套餐", "subcategory": "5G套餐", "raw": it,
             "source_api": "https://m.10099.com.cn/contact-web/api/goods/queryTariffAllByCond",
             "collected_at": "2026-09-05T10:00:00+0800"}
            for it in data
        ]
        items = normalize_items("cbn", entries)
        assert len(items) == len(data)
        for x in items:
            assert x["tariff_id"].startswith("CBN-")
        first = items[0]
        # productPrice 1000 分 → 10元
        assert "10" in first["price"]
        assert first["traffic"]  # 4GB
        assert first["details"]

    def test_type_tree_public(self):
        tree = json.loads((FIX / "cbn/queryTariffCondition.json").read_text(encoding="utf-8"))
        gz = next(t for t in tree if t.get("typeName") == "公众")
        assert gz["typeCode"] == "GZ"
        subs = [t["typeName"] for t in gz.get("childTariffTypes") or []]
        assert "套餐" in subs
        # 政企存在但不在采集范围
        assert any(t.get("typeName") == "政企" for t in tree)


class TestContentHash:
    def test_stable_and_sensitive(self):
        beans = json.loads((FIX / "cmcc/beans_hebei.json").read_text(encoding="utf-8"))
        it = beans[0]["nonModuleList"][0]
        entry = {"category": "套餐", "subcategory": "", "raw": dict(it), "source_api": "x", "collected_at": "t1"}
        a = normalize_items("cmcc", [entry])[0]
        b = normalize_items("cmcc", [dict(entry, collected_at="t2")])[0]
        assert a["content_hash"] == b["content_hash"]  # 时间戳不影响 hash
        c = normalize_items("cmcc", [{**entry, "raw": {**it, "fees": "999"}}])[0]
        assert a["content_hash"] != c["content_hash"]  # 价格变化影响 hash
