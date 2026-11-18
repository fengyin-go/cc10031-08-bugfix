"""提取规则准入与字段映射采集明细测试。

覆盖修复点：
1. 兜底逻辑不再拼接草稿/已测试规则 —— 未生效规则不参与采集
2. 每个字段映射记录实际命中的已生效规则版本
3. 无准入规则的站点保留阻塞状态，不静默降级
4. 站点只有草稿规则且历史批次可读取时，回退上一已生效版本继续采集
5. 明细台账版本受控写入：并发再次写入只接受一个版本，冲突推进处理终态
6. 明细持久保存，重复读取一致；规则启用后重新评估可解除阻塞
"""
from __future__ import annotations

import random
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.services.data_extract import DataExtractService
from app.store import store


@pytest.fixture(autouse=True)
def _deterministic_random():
    random.seed(20261118)


def _make_rule(site_structure: str, field: str, status: str,
               name: str | None = None, version: int = 1) -> dict:
    """登记一条测试提取规则（站点结构唯一，避免用例间相互污染）。"""
    rows = store.rows("data_extract")
    new_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
    rule = {
        "id": new_id,
        "规则名称": name or f"规则-{new_id}",
        "源页面结构": site_structure,
        "提取字段": field,
        "提取路径": "//div",
        "数据类型": "文本",
        "格式化": "无",
        "去空策略": "丢弃",
        "status": status,
        "pending": status != "已过期",
        "abnormal": False,
        "rule_version": version,
    }
    rows.append(rule)
    return rule


def _make_page_model(site: str, status: str = "已使用") -> dict:
    rows = store.rows("page_model")
    new_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
    model = {
        "id": new_id,
        "模型名称": f"模型-{site}",
        "目标站点": site,
        "页面结构": "表格",
        "动态元素": "",
        "分页模式": "none",
        "加载策略": "lazy",
        "版本号": "1.0",
        "status": status,
        "pending": False,
        "abnormal": False,
    }
    rows.append(model)
    return model


def _source_config(site: str, fields=("名称", "金额"), model_id=None) -> dict:
    return {
        "model_id": model_id,
        "model_name": "测试模型",
        "target_site": site,
        "version": "1.0",
        "pagination_mode": "none",
        "fields_to_extract": list(fields),
    }


def _publish_and_consume(svc: DataExtractService, site: str,
                         fields=("名称", "金额"), model_id=None) -> dict:
    store.channel.publish(
        topic="rpa.source_configured",
        source="page_model",
        payload=_source_config(site, fields, model_id),
    )
    return svc.consume_source_and_extract()


# ── 1. 兜底不再拼接草稿/已测试规则 ──

def test_no_fallback_to_draft_or_tested_rules():
    site = "甲A站点-仅草稿"
    _make_rule(site, "名称", "草稿")
    _make_rule(site, "金额", "已测试")
    svc = DataExtractService()

    # 旧的兜底会把草稿/已测试拼进字段列表；修复后只认已生效
    assert svc._get_active_rules_for_site(site) == []

    admission = svc.resolve_rule_admission(site)
    assert admission["admission"] == "已阻塞"
    assert admission["admitted_rules"] == []
    assert {r["status"] for r in admission["rejected_rules"]} == {"草稿", "已测试"}


def test_blocked_site_keeps_blocked_state_without_degradation():
    site = "乙B站点-阻塞"
    _make_rule(site, "名称", "草稿")
    model = _make_page_model(site)
    svc = DataExtractService()

    result = _publish_and_consume(svc, site, model_id=model["id"])
    batch = next(b for b in result["batches"] if b["site"] == site)
    assert batch["blocked"] is True
    assert batch["extracted"] == 0

    # 台账保留阻塞状态与阻塞原因，不静默降级
    detail = svc.get_site_detail(site)
    assert detail["process_state"] == "已阻塞"
    assert detail["admission"] == "已阻塞"
    assert "等待规则启用" in detail["blocked_reason"]
    assert detail["extracted"] == 0
    # 没有产生任何采集记录，草稿规则没有进入字段映射
    assert store.collect_records.list_by_batch(detail["batch_ref"]) == []
    assert all(not m["hit"] for m in detail["field_mappings"])
    assert all(
        r["status"] != "已生效" for r in detail["rejected_rules"])
    # 跨业务面同步待办：页面建模侧保持待处理
    assert store.find("page_model", model["id"])["pending"] is True


# ── 2. 字段映射记录命中的已生效规则版本 ──

def test_field_mapping_records_hit_rule_version():
    site = "丙C站点-版本"
    _make_rule(site, "名称、金额", "已生效", version=3)
    svc = DataExtractService()

    result = _publish_and_consume(svc, site, fields=("名称", "金额", "日期"))
    batch = next(b for b in result["batches"] if b["site"] == site)
    assert batch["blocked"] is False
    assert batch["extracted"] > 0

    detail = svc.get_site_detail(site)
    assert detail["process_state"] == "已准入"
    by_field = {m["field"]: m for m in detail["field_mappings"]}
    # 命中已生效规则的字段记录规则版本
    assert by_field["名称"]["hit"] is True
    assert by_field["名称"]["rule_version"] == "v3"
    assert by_field["名称"]["rule_status"] == "已生效"
    assert by_field["金额"]["rule_version"] == "v3"
    # 页面模型推断字段未命中规则时如实记录，不降级到草稿
    assert by_field["日期"]["hit"] is False
    assert by_field["日期"]["rule_version"] is None

    # 采集结果消息携带实际规则版本
    collected = [
        m for m in store.channel.snapshot(topic="rpa.data_collected")
        if m["payload"].get("site") == site
    ]
    assert collected, "应发布采集结果消息"
    payload = collected[-1]["payload"]
    assert payload["extract_rules_applied"] == 1
    assert all(v.startswith("v") for v in payload["rule_versions"].values())


def test_detail_persists_across_reads():
    site = "丁D站点-持久"
    _make_rule(site, "名称", "已生效")
    svc = DataExtractService()
    _publish_and_consume(svc, site)

    first = svc.get_site_detail(site)
    second = svc.get_site_detail(site)
    assert first == second
    assert first["version"] >= 1
    assert first["field_mappings"], "字段映射采集明细应持久保留"


# ── 3. 上一已生效版本回退 ──

def test_last_effective_version_fallback_when_only_drafts_remain():
    site = "戊E站点-历史版本"
    rule = _make_rule(site, "名称、金额", "已生效", version=2)
    svc = DataExtractService()

    # 规则已生效时解析一次，归档上一已生效版本快照
    admission = svc.resolve_rule_admission(site)
    assert admission["policy"] == "current"

    # 规则过期、新规则仍是草稿：历史批次仍可读取，回退上一已生效版本
    rule["status"] = "已过期"
    _make_rule(site, "名称", "草稿")

    admission = svc.resolve_rule_admission(site)
    assert admission["admission"] == "已准入(历史版本)"
    assert admission["policy"] == "last_effective"
    assert all(
        r["rule_status"] == "已生效(历史版本)"
        for r in admission["admitted_rules"]
    )
    assert any(r["status"] == "草稿" for r in admission["rejected_rules"])

    result = _publish_and_consume(svc, site)
    batch = next(b for b in result["batches"] if b["site"] == site)
    assert batch["blocked"] is False

    detail = svc.get_site_detail(site)
    assert detail["rule_policy"] == "last_effective"
    # 草稿规则禁止进入新明细：命中的只能是历史已生效版本
    hit_mappings = [m for m in detail["field_mappings"] if m["hit"]]
    assert hit_mappings, "历史版本应命中字段"
    assert all(m["rule_status"] == "已生效(历史版本)" for m in hit_mappings)
    assert all(m["rule_version"] == "v2" for m in hit_mappings)


# ── 4. 版本受控写入与冲突终态 ──

def test_concurrent_writes_accept_single_version():
    site = "己F站点-并发"
    ledger = store.field_mapping_ledger
    outcome, record = ledger.upsert(site, {"process_state": "已准入"}, 0)
    assert outcome == "ok" and record["version"] == 1

    outcomes: list[str] = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        outcome, _ = ledger.upsert(site, {"process_state": "已准入"}, 1)
        outcomes.append(outcome)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 并发再次写入只接受一个版本
    assert outcomes.count("ok") == 1
    assert all(o in ("conflict", "terminated") for o in outcomes if o != "ok")
    # 冲突把站点源配置处理状态推进终态
    assert ledger.get(site)["process_state"] == "冲突终止"
    # 终态下继续写入被拒绝，需显式解除
    outcome, _ = ledger.upsert(site, {"process_state": "已准入"}, 2)
    assert outcome == "terminated"
    assert ledger.reset_terminal(site) is True
    outcome, record = ledger.upsert(site, {"process_state": "已准入"}, 2)
    assert outcome == "ok" and record["version"] == 3


def test_service_conflict_moves_source_config_to_terminal():
    site = "庚G站点-冲突"
    _make_rule(site, "名称", "已生效")
    model = _make_page_model(site)
    svc = DataExtractService()

    admission = svc.resolve_rule_admission(site)
    detail = svc.build_field_mapping_detail(
        _source_config(site, model_id=model["id"]), admission, "BATC-T1")
    outcome, record = svc._persist_detail(site, detail)
    assert outcome == "ok" and record["version"] == 1

    # 另一写入者先把版本推进到 v2，本服务仍持有 v1 → 冲突
    svc._persist_detail(site, detail)
    outcome, _ = svc._persist_detail(site, detail, expected_version=1)
    assert outcome == "conflict"
    svc._apply_conflict_terminal(site, model["id"])

    # 冲突改变页面源配置的处理终态
    assert store.field_mapping_ledger.get(site)["process_state"] == "冲突终止"
    model_row = store.find("page_model", model["id"])
    assert model_row["status"] == "结构失效"
    assert model_row["abnormal"] is True


# ── 5. 重新评估与规则版本流转 ──

def test_reevaluate_unblocks_site_after_rule_enabled():
    site = "辛H站点-复评"
    rule = _make_rule(site, "名称", "草稿")
    svc = DataExtractService()

    _publish_and_consume(svc, site)
    assert svc.get_site_detail(site)["process_state"] == "已阻塞"

    # 规则启用后重新评估：阻塞解除，业务清单与结果台账一致
    rule["status"] = "已生效"
    result = svc.reevaluate_site(site)
    assert result["outcome"] == "ok"
    assert result["admission"] == "已准入"
    detail = svc.get_site_detail(site)
    assert detail["process_state"] == "已准入"
    assert detail["admitted_rules"][0]["rule_id"] == rule["id"]


def test_rule_version_bumps_on_status_transition():
    svc = DataExtractService()
    entry, missing = svc.create_entry({
        "规则名称": "版本流转规则",
        "源页面结构": "壬I站点-版本流转",
        "提取字段": "名称",
    })
    assert not missing
    assert entry["rule_version"] == 1

    entry, _ = svc.run_action(entry["id"], "测试提取")
    assert entry["rule_version"] == 2
    entry, _ = svc.run_action(entry["id"], "启用规则")
    assert entry["rule_version"] == 3


# ── 6. 工作台站点准入视图 ──

def test_site_admission_overview_shows_status_version_and_reason():
    site_ok = "癸J站点-视图正常"
    site_blocked = "寅K站点-视图阻塞"
    _make_rule(site_ok, "名称", "已生效", version=2)
    _make_rule(site_blocked, "名称", "草稿")
    _make_page_model(site_ok)
    _make_page_model(site_blocked)
    svc = DataExtractService()
    _publish_and_consume(svc, site_ok)
    _publish_and_consume(svc, site_blocked)

    overview = {item["site"]: item for item in svc.site_admission_overview()}
    assert overview[site_ok]["admission"] == "已准入"
    assert overview[site_ok]["admitted_rules"][0]["rule_version"] == 2
    assert overview[site_ok]["detail"]["process_state"] == "已准入"

    assert overview[site_blocked]["admission"] == "已阻塞"
    assert "等待规则启用" in overview[site_blocked]["blocked_reason"]
    assert overview[site_blocked]["detail"]["process_state"] == "已阻塞"
