"""IMA 同步增量逻辑测试（NO_CHANGE 不重复上传 / 变化才更新）。"""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from collector.normalize import _content_hash
from ima.sync import ImaSyncManager, STATE_PATH


def _item(op="cucc", tid="CUCC-26HE000001", name="测试套餐", price="129元/月"):
    base = {
        "operator": op, "operator_name": "中国联通", "province": "河北", "audience": "个人",
        "category": "套餐", "subcategory": "", "tariff_id": tid, "name": name,
        "price": price, "traffic": "30GB", "voice": "500", "sms": "0",
        "validity": "长期", "eligibility": "河北联通用户", "description": "d",
        "details": {"方案编号": tid}, "source_url": "u", "source_api": "a",
        "collected_at": "t1",
    }
    base["content_hash"] = _content_hash(base)
    return base


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    """构造临时 data 目录与 state。"""
    from collector import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    import ima.sync as sync_mod
    monkeypatch.setattr(sync_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(sync_mod, "STATE_PATH", tmp_path / "ima-state.json")
    return tmp_path


def _write_latest(repo, items, generated="2026-09-05T10:00:00"):
    (repo / "cucc").mkdir(parents=True, exist_ok=True)
    (repo / "cucc" / "latest.json").write_text(
        json.dumps({"items": items, "generated_at": generated}, ensure_ascii=False), encoding="utf-8"
    )


class TestIncrementalSync:
    def test_plan_hash_stable_across_time(self, repo):
        """采集时间变化不影响分类 hash（NO_CHANGE 核心保障）。"""
        items = [_item(tid=f"CUCC-26HE{i:08d}") for i in range(5)]
        _write_latest(repo, items, generated="2026-09-05T10:00:00")
        m = ImaSyncManager(dry_run=True)
        plan1 = m._plan_operator("cucc")

        _write_latest(repo, items, generated="2027-01-01T00:00:00")
        m2 = ImaSyncManager(dry_run=True)
        plan2 = m2._plan_operator("cucc")
        assert plan1[0]["hash"] == plan2[0]["hash"]

    def test_plan_hash_changes_when_data_changes(self, repo):
        items = [_item(tid=f"CUCC-26HE{i:08d}") for i in range(5)]
        _write_latest(repo, items)
        m = ImaSyncManager(dry_run=True)
        h1 = m._plan_operator("cucc")[0]["hash"]

        items2 = items + [_item(tid="CUCC-26HE99999999", name="新套餐")]
        _write_latest(repo, items2)
        m2 = ImaSyncManager(dry_run=True)
        h2 = m2._plan_operator("cucc")[0]["hash"]
        assert h1 != h2

    def test_hash_changes_when_price_modified(self, repo):
        items = [_item(tid="CUCC-26HE00000001")]
        _write_latest(repo, items)
        m = ImaSyncManager(dry_run=True)
        h1 = m._plan_operator("cucc")[0]["hash"]
        items2 = [_item(tid="CUCC-26HE00000001", price="199元/月")]
        _write_latest(repo, items2)
        m2 = ImaSyncManager(dry_run=True)
        h2 = m2._plan_operator("cucc")[0]["hash"]
        assert h1 != h2

    def test_sync_skips_unchanged(self, repo):
        items = [_item(tid="CUCC-26HE00000001")]
        _write_latest(repo, items)
        m = ImaSyncManager(dry_run=True)
        plan = m._plan_operator("cucc")
        # 预置 state = 当前 hash
        m.state.setdefault("operators", {}).setdefault("cucc", {"files": {}})
        m.state["operators"]["cucc"]["files"][plan[0]["file_name"]] = {"hash": plan[0]["hash"], "media_id": "m1"}
        summary = {"operators": {"cucc": {"status": "PASS", "latest_promoted": True, "diff_status": "NO_CHANGE"}}}
        report = m.sync(["cucc"], summary)
        # 无变化 → 不应进入上传
        assert report["operators"]["cucc"]["sync"] == "NO_CHANGE"

    def test_sync_skips_failed_integrity(self, repo):
        items = [_item(tid="CUCC-26HE00000001")]
        _write_latest(repo, items)
        m = ImaSyncManager(dry_run=True)
        summary = {"operators": {"cucc": {"status": "FAIL", "diff_status": "CHANGED"}}}
        report = m.sync(["cucc"], summary)
        assert report["operators"]["cucc"]["sync"] == "SKIPPED"

    def test_upload_uses_timestamp_on_duplicate(self, repo):
        """同名文件已存在 → 时间戳版本名（官方不支持替换）。"""
        items = [_item(tid="CUCC-26HE00000001")]
        _write_latest(repo, items)
        m = ImaSyncManager(dry_run=True)
        uploaded = {}

        class FakeClient:
            def check_repeated_names(self, names, kb_id, media_type=7, folder_id=None):
                return {n: True for n in names}  # 全部重名

            def create_media(self, file_name, file_size, content_type, kb_id, file_ext):
                assert file_name.endswith(".md") and "_" in file_name  # 时间戳版本
                return {"media_id": "md-1", "cos_credential": {"cos_key": "k", "bucket_name": "b", "region": "r", "secret_id": "s", "secret_key": "k", "token": "t", "start_time": 1, "expired_time": 2}}

            def add_knowledge_file(self, media_id, title, kb_id, file_name, file_size, cos_key):
                uploaded["title"] = title
                return {}

        m.client = FakeClient()
        m.kb_id = "KB1"
        with patch("ima.sync.cos_upload"):
            media_id, final_name = m._upload_file("河北联通_套餐.md", "# content")
        assert "_20" in final_name  # 时间戳后缀
        assert final_name.endswith(".md")
