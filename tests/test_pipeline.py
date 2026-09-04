"""integrity / diff / storage / markdown / IMA client / COS 测试。"""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from collector.diff import diff_tariffs
from collector.integrity import check_integrity
from ima.client import ImaClient, ImaApiError
from ima.cos import build_authorization
from ima.markdown import category_markdown, content_hash

FIX = Path(__file__).resolve().parent.parent / "fixtures"


def _item(op="cmcc", tid="CMCC-26HE000001", name="测试套餐", price="129元/月", **kw):
    base = {
        "operator": op, "operator_name": "中国移动", "province": "河北", "audience": "个人",
        "category": "套餐", "subcategory": "", "tariff_id": tid, "name": name,
        "price": price, "traffic": "30GB", "voice": "500分钟", "sms": "0",
        "validity": "长期", "eligibility": "河北移动用户", "description": "描述",
        "details": {"方案编号": tid}, "source_url": "u", "source_api": "a",
        "collected_at": "t",
    }
    base.update(kw)
    from collector.normalize import _content_hash
    base["content_hash"] = _content_hash(base)
    return base


def _outcome(op="cmcc", ev_extra=None, cat_results=None, errors=None):
    ev = {
        "province_ok": True,
        "scope": "河北 × 个人",
        "category_results": cat_results or [{"category": "套餐", "expected": 10, "collected": 10, "error": ""}],
    }
    if op == "cbn":
        ev["personal_type"] = {"typeName": "公众", "typeCode": "GZ"}
    if op == "cmcc":
        ev["personal"] = {"url_is_personal_zone": True, "page_shows_personal_tab": True}
    if ev_extra:
        ev.update(ev_extra)
    return {"evidence": ev, "categories": [], "errors": errors or []}


# ═════════ integrity ═════════

class TestIntegrity:
    def test_pass(self):
        items = [_item(tid=f"CMCC-26HE{i:06d}") for i in range(50)]
        report = check_integrity("cmcc", _outcome(), items, previous=None)
        assert report["overall"] == "PASS"
        assert all(c["status"] == "PASS" for c in report["checks"])

    def test_fail_province(self):
        items = [_item(tid=f"CMCC-26HE{i:06d}") for i in range(50)]
        report = check_integrity("cmcc", _outcome(ev_extra={"province_ok": False}), items, None)
        assert report["overall"] == "FAIL"
        assert any(c["check"] == "province_hebei" and c["status"] == "FAIL" for c in report["checks"])

    def test_fail_category_mismatch(self):
        items = [_item(tid=f"CMCC-26HE{i:06d}") for i in range(10)]
        cats = [{"category": "套餐", "expected": 100, "collected": 10, "error": ""}]
        report = check_integrity("cmcc", _outcome(cat_results=cats), items, None)
        assert report["overall"] == "FAIL"
        assert any(c["check"] == "official_totals" and c["status"] == "FAIL" for c in report["checks"])

    def test_drop_protection(self):
        prev = [_item(tid=f"CMCC-26HE{i:06d}") for i in range(500)]
        cur = [_item(tid=f"CMCC-26HE{i:06d}") for i in range(80)]  # 84% 下降
        # 官方 total 与实际一致（80==80）→ 应豁免？
        cats = [{"category": "套餐", "expected": 80, "collected": 80, "error": ""}]
        report = check_integrity("cmcc", _outcome(cat_results=cats), cur, prev)
        # totals 一致 → 判定为官方正常下线 → PASS
        drop_check = next(c for c in report["checks"] if c["check"] == "no_abnormal_drop")
        assert drop_check["status"] == "PASS"
        assert "官方正常下线" in drop_check["detail"]

    def test_drop_protection_with_mismatch_fails(self):
        prev = [_item(tid=f"CMCC-26HE{i:06d}") for i in range(500)]
        cur = [_item(tid=f"CMCC-26HE{i:06d}") for i in range(80)]
        cats = [{"category": "套餐", "expected": 500, "collected": 80, "error": ""}]  # total 不符
        report = check_integrity("cmcc", _outcome(cat_results=cats), cur, prev)
        drop_check = next(c for c in report["checks"] if c["check"] == "no_abnormal_drop")
        assert drop_check["status"] == "FAIL"

    def test_audience_cbn(self):
        items = [_item(op="cbn", tid=f"CBN-26JT{i:06d}") for i in range(3)]
        ev = {
            "province_ok": True, "scope": "s",
            "personal_type": {"typeName": "政企", "typeCode": "ZQ"},
            "category_results": [{"category": "套餐", "expected": 1, "collected": 1, "error": ""}],
        }
        report = check_integrity("cbn", {"evidence": ev, "categories": [], "errors": []}, items, None)
        assert any(c["check"] == "audience_personal" and c["status"] == "FAIL" for c in report["checks"])


# ═════════ diff ═════════

class TestDiff:
    def test_added_removed_modified(self):
        prev = [_item(tid="A", name="A套餐", price="10元/月"), _item(tid="B", name="B套餐"), _item(tid="C", name="C套餐", price="30元/月")]
        cur = [_item(tid="A", name="A套餐", price="20元/月"), _item(tid="C", name="C套餐", price="30元/月"), _item(tid="D", name="D套餐")]
        d = diff_tariffs(cur, prev)
        assert d["added"]["count"] == 1 and d["added"]["items"][0]["tariff_id"] == "D"
        assert d["removed"]["count"] == 1 and d["removed"]["items"][0]["tariff_id"] == "B"
        assert d["modified"]["count"] == 1 and d["modified"]["items"][0]["tariff_id"] == "A"
        assert d["modified"]["items"][0]["fields"] == ["price"]
        assert d["unchanged"]["count"] == 1
        assert d["status"] == "CHANGED" and d["has_change"]

    def test_no_change(self):
        items = [_item(tid="A"), _item(tid="B")]
        d = diff_tariffs(items, items)
        assert d["status"] == "NO_CHANGE"
        assert not d["has_change"]

    def test_first_run_all_added(self):
        items = [_item(tid="A")]
        d = diff_tariffs(items, None)
        assert d["added"]["count"] == 1
        assert d["previous_count"] == 0

    def test_deletion_requires_complete_current(self):
        """删除检测建立在本次数据完整前提下——由 sync 侧 gate 保证（这里验证 diff 纯函数性）。"""
        prev = [_item(tid=f"X{i}") for i in range(100)]
        cur = []
        d = diff_tariffs(cur, prev)
        assert d["removed"]["count"] == 100


# ═════════ markdown ═════════

class TestMarkdown:
    def test_category_markdown(self):
        items = [
            _item(tid="CMCC-1", name="套餐一", subcategory="5G"),
            _item(tid="CMCC-2", name="套餐二", subcategory="5G"),
            _item(tid="CMCC-3", name="套餐三", subcategory=""),
        ]
        fname, content = category_markdown("cmcc", "河北移动", "套餐", items, "2026-09-05")
        assert fname == "河北移动_套餐.md"
        assert content.startswith("# 河北移动 · 套餐")
        assert "套餐一（CMCC-1）" in content
        assert "| 方案编号 |" in content
        assert "共 3 条资费" in content
        assert "## 5G（2 条）" in content
        h1 = content_hash(content)
        h2 = content_hash(content)
        assert h1 == h2
        assert content_hash(content + "x") != h1


# ═════════ IMA client（mock 网络层） ═════════

class TestImaClient:
    def _client(self):
        return ImaClient("test-client-id", "test-api-key")

    def _mock_urlopen(self, responses):
        """返回一个 fake urlopen，按调用次数返回 responses。"""
        calls = []

        class FakeResp:
            def __init__(self, payload):
                self._payload = payload

            def read(self):
                return json.dumps(self._payload).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake(req, timeout=None):
            calls.append(req)
            payload = responses[min(len(calls) - 1, len(responses) - 1)]
            if isinstance(payload, Exception):
                raise payload
            return FakeResp(payload)

        return fake, calls

    def test_post_success(self):
        fake, calls = self._mock_urlopen([{"code": 0, "msg": "ok", "data": {"x": 1}}])
        with patch("ima.client.urllib.request.urlopen", fake):
            resp = self._client().post("openapi/wiki/v1/get_knowledge_base", {"ids": ["k"]})
        assert resp["data"] == {"x": 1}
        req = calls[0]
        assert req.full_url == "https://ima.qq.com/openapi/wiki/v1/get_knowledge_base"
        hdrs = {k.lower(): v for k, v in req.headers.items()}
        assert hdrs.get("ima-openapi-clientid") == "test-client-id"
        assert hdrs.get("ima-openapi-apikey") == "test-api-key"
        assert hdrs.get("content-type") == "application/json"
        body = json.loads(req.data.decode("utf-8"))
        assert body == {"ids": ["k"]}

    def test_post_business_error(self):
        fake, _ = self._mock_urlopen([{"code": 110011, "msg": "参数非法", "data": {}}])
        with patch("ima.client.urllib.request.urlopen", fake):
            with pytest.raises(ImaApiError) as e:
                self._client().post("openapi/wiki/v1/x", {})
        assert e.value.code == 110011

    def test_post_retry_on_rate_limit(self):
        responses = [
            {"code": 110021, "msg": "请求频控", "data": {}},
            {"code": 0, "msg": "ok", "data": {}},
        ]
        fake, calls = self._mock_urlopen(responses)
        with patch("ima.client.urllib.request.urlopen", fake):
            resp = self._client().post("p", {})
        assert resp["code"] == 0
        assert len(calls) == 2

    def test_kb_field_variants(self):
        fake, _ = self._mock_urlopen([
            {"code": 0, "msg": "ok", "data": {"info_list": [
                {"kb_id": "KB1", "kb_name": "河北运营商资费"},
                {"kb_id": "KB2", "kb_name": "其他"},
            ], "is_end": True}}
        ])
        with patch("ima.client.urllib.request.urlopen", fake):
            kbs = self._client().search_knowledge_base("河北运营商资费")
        assert kbs[0]["id"] == "KB1" and kbs[0]["name"] == "河北运营商资费"

    def test_missing_credentials(self):
        import os
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(RuntimeError):
                ImaClient()


# ═════════ COS 签名 ═════════

class TestCos:
    def test_authorization_format(self):
        auth = build_authorization(
            "SECRET_ID", "SECRET_KEY", "PUT", "/key.md",
            {"content-length": "100", "host": "b.cos.ap-guangzhou.myqcloud.com"},
            1700000000, 1700003600,
        )
        assert auth.startswith("q-sign-algorithm=sha1&")
        assert "q-ak=SECRET_ID" in auth
        assert "q-sign-time=1700000000;1700003600" in auth
        assert "q-header-list=content-length;host" in auth
        assert "q-url-param-list=" in auth
        assert auth.split("q-signature=")[1]

    def test_signature_deterministic(self):
        args = ("SID", "SKEY", "PUT", "/k", {"host": "h"}, 1, 2)
        assert build_authorization(*args) == build_authorization(*args)
