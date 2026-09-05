"""IMA 笔记模式同步测试（原地编辑 / NO_CHANGE 不写入 / 完整性闸门 / 幂等恢复）。"""
import json
from pathlib import Path

import pytest

from collector.normalize import _content_hash
from ima.sync import ImaSyncManager, STATE_PATH


def _item(op="cucc", tid="CUCC-26HE000001", name="测试套餐", price="129元/月", category="套餐"):
    base = {
        "operator": op, "operator_name": "中国联通", "province": "河北", "audience": "个人",
        "category": category, "subcategory": "", "tariff_id": tid, "name": name,
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
        json.dumps({"items": items, "generated_at": generated, "promoted_at": generated}, ensure_ascii=False),
        encoding="utf-8",
    )


def _write_diff(repo, added=(), removed=(), modified=()):
    (repo / "cucc" / "diff.json").write_text(
        json.dumps(
            {
                "status": "CHANGED" if (added or removed or modified) else "NO_CHANGE",
                "added": {"count": len(added), "items": list(added)},
                "removed": {"count": len(removed), "items": list(removed)},
                "modified": {"count": len(modified), "items": list(modified)},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


class FakeClient:
    """记录全部写操作的假客户端。"""

    def __init__(self):
        self.notes = {}          # note_id -> content
        self.next_id = 1000
        self.kb_links = []       # (note_id, title)
        self.search_results = []  # _find_existing_note 恢复用
        self.append_error = None

    def import_doc(self, content):
        nid = str(self.next_id)
        self.next_id += 1
        self.notes[nid] = content
        return nid

    def append_doc(self, note_id, content):
        if self.append_error:
            raise self.append_error
        assert note_id in self.notes, "append 到不存在的笔记"
        self.notes[note_id] += content

    def get_doc_content(self, note_id):
        return self.notes.get(note_id, "")

    def search_notes(self, query, by_content=False, limit=20):
        return self.search_results

    def add_knowledge_note(self, note_id, title, kb_id):
        if (note_id, title) in self.kb_links:
            return {"already_linked": True}
        self.kb_links.append((note_id, title))
        return {}

    def find_knowledge_base(self, name):
        return {"id": "KB1", "name": name}


def _manager(repo, client=None, dry_run=False):
    m = ImaSyncManager(dry_run=dry_run)
    if client is None:
        client = FakeClient()
    m.client = client
    m.kb_id = "KB1"
    return m, client


class TestPlanHash:
    def test_plan_hash_stable_across_time(self, repo):
        """采集时间变化不影响分类 hash（NO_CHANGE 核心保障）。"""
        items = [_item(tid=f"CUCC-26HE{i:08d}") for i in range(5)]
        _write_latest(repo, items, generated="2026-09-05T10:00:00")
        m, _ = _manager(repo, dry_run=True)
        plan1 = m._plan_operator("cucc")

        _write_latest(repo, items, generated="2027-01-01T00:00:00")
        m2, _ = _manager(repo, dry_run=True)
        plan2 = m2._plan_operator("cucc")
        assert plan1[0]["hash"] == plan2[0]["hash"]

    def test_plan_hash_changes_when_data_changes(self, repo):
        items = [_item(tid=f"CUCC-26HE{i:08d}") for i in range(5)]
        _write_latest(repo, items)
        m, _ = _manager(repo, dry_run=True)
        h1 = m._plan_operator("cucc")[0]["hash"]

        items2 = items + [_item(tid="CUCC-26HE99999999", name="新套餐")]
        _write_latest(repo, items2)
        m2, _ = _manager(repo, dry_run=True)
        h2 = m2._plan_operator("cucc")[0]["hash"]
        assert h1 != h2

    def test_plan_hash_changes_when_price_modified(self, repo):
        items = [_item(tid="CUCC-26HE00000001")]
        _write_latest(repo, items)
        m, _ = _manager(repo, dry_run=True)
        h1 = m._plan_operator("cucc")[0]["hash"]
        items2 = [_item(tid="CUCC-26HE00000001", price="199元/月")]
        _write_latest(repo, items2)
        m2, _ = _manager(repo, dry_run=True)
        h2 = m2._plan_operator("cucc")[0]["hash"]
        assert h1 != h2


class TestNotesSync:
    def test_migration_creates_note_with_baseline(self, repo):
        """无笔记状态 → 建笔记（全量基线）+ 挂库，即使 diff NO_CHANGE（迁移语义）。"""
        items = [_item(tid=f"CUCC-26HE{i:08d}") for i in range(3)]
        _write_latest(repo, items)
        _write_diff(repo)
        m, client = _manager(repo)
        summary = {"operators": {"cucc": {"status": "PASS", "latest_promoted": True, "diff_status": "NO_CHANGE"}}}
        report = m.sync(["cucc"], summary)

        res = report["operators"]["cucc"]
        assert res["sync"] == "UPDATED"
        assert len(res["created"]) == 1
        assert len(client.notes) == 1
        content = next(iter(client.notes.values()))
        assert "# 河北联通 · 套餐（实时更新）" in content
        assert "c-cctoima:cucc:套餐" in content          # 标识行
        assert "阅读指引" in content                        # 增量阅读指引
        assert "测试套餐" in content
        assert client.kb_links[0][1] == "河北联通_套餐（实时更新）"

    def test_no_change_skips_writes(self, repo):
        """hash 一致 → 不建/不追加任何写操作。"""
        items = [_item(tid="CUCC-26HE00000001")]
        _write_latest(repo, items)
        m, client = _manager(repo)
        plan = m._plan_operator("cucc")
        cats = m.state.setdefault("operators", {}).setdefault("cucc", {"categories": {}})["categories"]
        cats["套餐"] = {
            "note_id": "N1", "title": plan[0]["title"], "hash": plan[0]["hash"], "count": 1
        }
        summary = {"operators": {"cucc": {"status": "PASS", "latest_promoted": True, "diff_status": "NO_CHANGE"}}}
        report = m.sync(["cucc"], summary)
        assert report["operators"]["cucc"]["sync"] == "NO_CHANGE"
        assert client.notes == {} and client.kb_links == []

    def test_change_appends_update_section_in_place(self, repo):
        """数据变化 → 原笔记末尾追加【增量更新】段，不新建笔记/不新挂库。"""
        old = [_item(tid="CUCC-26HE00000001"), _item(tid="CUCC-26HE00000002", name="将下架")]
        _write_latest(repo, old)
        m, client = _manager(repo)
        plan = m._plan_operator("cucc")
        cats = m.state.setdefault("operators", {}).setdefault("cucc", {"categories": {}})["categories"]
        cats["套餐"] = {
            "note_id": "N1", "title": plan[0]["title"], "hash": "OLD", "count": 2
        }
        client.notes["N1"] = "# 河北联通 · 套餐（实时更新）\n\n基线内容"

        new = [_item(tid="CUCC-26HE00000001", price="199元/月"), _item(tid="CUCC-26HE00999999", name="新上架")]
        _write_latest(repo, new)
        _write_diff(
            repo,
            added=[{"tariff_id": "CUCC-26HE00999999", "name": "新上架", "category": "套餐"}],
            removed=[{"tariff_id": "CUCC-26HE00000002", "name": "将下架", "category": "套餐"}],
            modified=[{"tariff_id": "CUCC-26HE00000001", "name": "测试套餐", "category": "套餐", "fields": ["price"]}],
        )
        summary = {"operators": {"cucc": {"status": "PASS", "latest_promoted": True, "diff_status": "CHANGED"}}}
        report = m.sync(["cucc"], summary)

        res = report["operators"]["cucc"]
        assert res["sync"] == "UPDATED"
        assert res["created"] == []                       # 不新建
        assert len(res["appended"]) == 1                  # 原地追加
        assert len(client.notes) == 1                     # 仍只有一篇笔记
        content = client.notes["N1"]
        assert "基线内容" in content                       # 原内容保留
        assert "【增量更新" in content
        assert "新上架" in content and "199元/月" in content
        assert "将下架" in content and "已停售" in content
        assert "当前有效清单" in content                   # 最新全量快照
        assert client.kb_links == []                      # 不重复挂库
        # 状态推进到新 hash
        assert m.state["operators"]["cucc"]["categories"]["套餐"]["hash"] == m._plan_operator("cucc")[0]["hash"]
        assert m.state["operators"]["cucc"]["categories"]["套餐"]["updates"] == 1

    def test_integrity_fail_skips_everything(self, repo):
        items = [_item(tid="CUCC-26HE00000001")]
        _write_latest(repo, items)
        m, client = _manager(repo)
        summary = {"operators": {"cucc": {"status": "FAIL", "diff_status": "CHANGED"}}}
        report = m.sync(["cucc"], summary)
        assert report["operators"]["cucc"]["sync"] == "SKIPPED"
        assert client.notes == {}

    def test_repeated_kb_link_is_idempotent(self, repo):
        """部分失败恢复：既有笔记被找回并复用，重复挂库不报错。"""
        items = [_item(tid="CUCC-26HE00000001")]
        _write_latest(repo, items)
        m, client = _manager(repo)
        # 模拟上次运行：笔记已建已挂库，但 state 丢失
        nid = client.import_doc("# 河北联通 · 套餐（实时更新）\n> 笔记标识：c-cctoima:cucc:套餐\n")
        client.search_results = [{"note_id": nid, "title": "河北联通 · 套餐（实时更新）"}]

        summary = {"operators": {"cucc": {"status": "PASS", "latest_promoted": True, "diff_status": "NO_CHANGE"}}}
        report = m.sync(["cucc"], summary)
        res = report["operators"]["cucc"]
        assert res["sync"] == "UPDATED"
        assert len(res["created"]) == 1
        assert res["created"][0]["note_id"] == nid       # 复用，不新建
        assert len(client.notes) == 1
        client.add_knowledge_note(nid, "河北联通_套餐（实时更新）", "KB1")  # 再次挂库 → already_linked 不抛错

    def test_dry_run_makes_no_api_calls(self, repo):
        items = [_item(tid="CUCC-26HE00000001")]
        _write_latest(repo, items)
        m, client = _manager(repo, dry_run=True)
        summary = {"operators": {"cucc": {"status": "PASS", "latest_promoted": True, "diff_status": "NO_CHANGE"}}}
        report = m.sync(["cucc"], summary)
        assert report["operators"]["cucc"]["sync"] == "DRY_RUN_PLAN"
        assert client.notes == {} and client.kb_links == []

    def test_append_failure_recorded_not_fatal(self, repo):
        """单分类追加失败：记录 FAILED，不影响状态推进为其他分类。"""
        items = [_item(tid="CUCC-26HE00000001"),
                 _item(tid="CUCC-26HE00000002", category="加装包", name="加油包")]
        _write_latest(repo, items)
        m, client = _manager(repo)
        plan = m._plan_operator("cucc")
        cats = m.state.setdefault("operators", {}).setdefault("cucc", {"categories": {}})["categories"]
        for p in plan:
            cats[p["category"]] = {"note_id": f"N-{p['category']}", "title": p["title"], "hash": "OLD", "count": 1}
            client.notes[f"N-{p['category']}"] = "base"
        from ima.client import ImaApiError
        client.append_error = ImaApiError(110011, "下游逻辑错误", "append_doc")

        _write_diff(repo, modified=[{"tariff_id": "CUCC-26HE00000001", "name": "测试套餐", "category": "套餐", "fields": ["price"]}])
        summary = {"operators": {"cucc": {"status": "PASS", "latest_promoted": True, "diff_status": "CHANGED"}}}
        report = m.sync(["cucc"], summary)
        assert report["operators"]["cucc"]["sync"] == "FAILED"

    def test_category_retired_gets_closing_append(self, repo):
        """分类整体消失 → 追加下架收尾段。"""
        old = [_item(tid="CUCC-26HE00000001"),
               _item(tid="CUCC-26HE00000002", name="停售条目", category="停售包")]
        _write_latest(repo, old)
        m, client = _manager(repo)
        cats = m.state.setdefault("operators", {}).setdefault("cucc", {"categories": {}})["categories"]
        cats["套餐"] = {"note_id": "N-套餐", "title": "河北联通_套餐（实时更新）", "hash": "H1", "count": 1}
        cats["停售包"] = {"note_id": "N-停售包", "title": "河北联通_停售包（实时更新）", "hash": "H2", "count": 1}
        client.notes["N-套餐"] = "base"
        client.notes["N-停售包"] = "base"

        new = [_item(tid="CUCC-26HE00000001")]
        _write_latest(repo, new)
        _write_diff(repo, removed=[{"tariff_id": "CUCC-26HE00000002", "name": "停售条目", "category": "停售包"}])
        summary = {"operators": {"cucc": {"status": "PASS", "latest_promoted": True, "diff_status": "CHANGED"}}}
        report = m.sync(["cucc"], summary)
        content = client.notes["N-停售包"]
        assert "本分类已全部下架" in content
        assert "停售条目" in content
        assert m.state["operators"]["cucc"]["categories"]["停售包"]["retired"] is True
