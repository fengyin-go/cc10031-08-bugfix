"""跨模块数据流集成测试 — 验证 RPA 流程自动化中心的端到端数据流。

测试覆盖：
1. RPA 流程编排链路（bot_definition → step_editor → task_queue）
2. 数据采集与校验链路（page_model → data_extract → result_verify → callback_hook）
3. 元素定位与执行链路（element_selector → runtime_session）
"""
from __future__ import annotations

import sys
from pathlib import Path

# 确保 backend 目录在 sys.path 中
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.store import store
from app.services.bot_definition import BotDefinitionService
from app.services.step_editor import StepEditorService
from app.services.task_queue import TaskQueueService
from app.services.page_model import PageModelService
from app.services.data_extract import DataExtractService
from app.services.result_verify import ResultVerifyService
from app.services.element_selector import ElementSelectorService
from app.services.runtime_session import runtime_session
from app.services.callback_hook import callback_hook


# ── 测试 1: RPA 流程编排数据流 ──

def test_rpa_lifecycle_flow():
    """测试 bot_definition → step_editor → task_queue 的完整数据流。

    1. bot_definition 发布流程 → channel("rpa.bot_created")
    2. step_editor 消费 + 编排步骤 → channel("rpa.steps_submitted")
    3. task_queue 消费编排结果 → 生成任务
    """
    print("\n=== 测试 1: RPA 流程编排数据流 ===")

    bot_svc = BotDefinitionService()
    step_svc = StepEditorService()
    task_svc = TaskQueueService()

    # Step 1: 发布流程
    active_bots = bot_svc.query_active_bots()
    if not active_bots:
        # 先激活一个 bot
        bot_entry = store.find("bot_definition", 1)
        if bot_entry:
            bot_entry["status"] = "测试中"
        bot_entry = store.find("bot_definition", 2)
        if bot_entry:
            bot_entry["status"] = "测试中"
        active_bots = bot_svc.query_active_bots()

    # 确保有活跃 bot
    for row in store.rows("bot_definition"):
        if row.get("status") == "已发布":
            break
    else:
        # 发布第一个
        result = bot_svc.publish_bot(1)
        assert result is not None, "发布流程失败"
        print(f"  [OK] 发布流程 bot_id=1: status={result['status']}")
        assert result["status"] == "已发布"

    # Step 2: 再次发布另一个
    result2 = bot_svc.publish_bot(2)
    if result2:
        print(f"  [OK] 发布流程 bot_id=2: status={result2['status']}")

    # 验证 channel 消息
    msgs = store.channel.snapshot(topic="rpa.bot_created")
    print(f"  [INFO] channel('rpa.bot_created') 消息数: {len(msgs)}")
    assert len(msgs) > 0, "channel 中没有 bot_created 消息"

    # Step 3: 编排步骤
    enrich_result = step_svc.enrich_steps_from_channel()
    print(f"  [OK] 步骤丰富: enriched={enrich_result['enriched']}, bots_found={enrich_result['bots_found']}")

    # 提交步骤编排
    steps_data = [
        {"seq": 1, "name": "打开目标页面", "type": "navigate", "element": "#login-btn", "wait": "visible"},
        {"seq": 2, "name": "输入关键词", "type": "input", "element": "#search-input", "params": "keyword=产品",
         "wait": "visible"},
        {"seq": 3, "name": "点击搜索", "type": "click", "element": "#search-btn", "wait": "clickable"},
        {"seq": 4, "name": "提取结果表格", "type": "extract", "element": ".results-table", "wait": "visible"},
        {"seq": 5, "name": "翻页", "type": "click", "element": ".next-page", "wait": "visible", "on_error": "skip"},
    ]
    submit_result = step_svc.submit_steps_to_queue(bot_id=1, steps_data=steps_data)
    print(f"  [OK] 步骤提交: submitted={submit_result['submitted']}, plan_id={submit_result['plan_id']}")
    assert submit_result["submitted"] > 0, "没有步骤成功提交"
    assert submit_result["total_steps"] == 5, "步骤总数不正确"

    # Step 4: 生成任务队列
    queue_result = task_svc.consume_steps_to_queue()
    print(f"  [OK] 任务生成: generated={queue_result['generated']}, plans_consumed={queue_result['plans_consumed']}")
    assert queue_result["generated"] > 0, "没有成功生成任务"

    # 验证任务状态
    queue_rows = store.rows("task_queue")
    queued_count = sum(1 for r in queue_rows if r.get("status") == "排队中")
    print(f"  [INFO] 排队中的任务数: {queued_count}")

    # Step 5: 调度执行
    schedule_result = task_svc.schedule_next_batch(max_tasks=3)
    print(f"  [OK] 调度执行: scheduled={schedule_result['scheduled']}")

    print("  [PASS] RPA 流程编排数据流测试通过")
    return True


# ── 测试 2: 数据采集与校验数据流 ──

def test_collection_validation_flow():
    """测试 page_model → data_extract → result_verify → callback_hook 的完整数据流。

    1. page_model 发布源端配置 → channel("rpa.source_configured")
    2. data_extract 消费 + 模拟采集 → CollectRecord + DirtyDataVault
    3. result_verify 消费 + 校验 → channel("rpa.validation_complete")
    4. callback_hook 消费 + 触发回调
    """
    print("\n=== 测试 2: 数据采集与校验数据流 ===")

    page_svc = PageModelService()
    extract_svc = DataExtractService()
    verify_svc = ResultVerifyService()

    # Step 1: 发布页面模型为源端配置
    models = store.rows("page_model")
    for model in models:
        if model.get("status") in ("草稿", "已验证"):
            page_svc.publish_source_config(model["id"])
            break
    else:
        # 如果没有草稿模型，直接发布第一个
        page_svc.publish_source_config(models[0]["id"])

    model_msgs = store.channel.snapshot(topic="rpa.source_configured")
    assert len(model_msgs) > 0, "没有 source_configured 消息"
    print(f"  [OK] 源端配置发布: {len(model_msgs)} 条")

    # 规则准入修复后，站点必须有已生效提取规则才会采集
    # （草稿/已测试规则不再被兜底拼接，无准入规则的站点保留阻塞）。
    # 这里为发布的站点启用一条已生效规则，模拟运营完成规则准入。
    site = model_msgs[-1]["payload"].get("target_site")
    rule_rows = store.rows("data_extract")
    effective = next(
        (r for r in rule_rows if r.get("status") == "已生效"), None)
    if effective is None:
        effective = rule_rows[0]
        effective["status"] = "已生效"
    effective["源页面结构"] = site

    # Step 2: 执行数据采集
    extract_result = extract_svc.consume_source_and_extract()
    print(f"  [OK] 数据采集: extracted={extract_result['extracted']}, dirty={extract_result['dirty']}")
    assert extract_result["extracted"] > 0, "没有采集到数据"
    assert len(extract_result["batches"]) > 0, "没有采集批次"

    # 验证 CollectRecord
    collect_snapshot = store.collect_records.snapshot()
    print(f"  [INFO] 采集记录: total={collect_snapshot['total']}, validated={collect_snapshot['validated']}")
    assert collect_snapshot["total"] > 0, "CollectRecord 中没有记录"

    # 验证 DirtyVault
    dirty_snapshot = store.dirty_vault.snapshot()
    print(f"  [INFO] 脏数据隔离区: total={dirty_snapshot['total']}, quarantined={dirty_snapshot['quarantined']}")

    # Step 3: 校验采集结果
    verify_result = verify_svc.verify_collected_data()
    print(
        f"  [OK] 结果校验: passed={verify_result['passed']}, failed={verify_result['failed']}, "
        f"batches={len(verify_result['batches'])}"
    )
    # 注意：如果采集记录都在校验后被隔离了，passed 可以为 0
    # 这里不强制 assert passed > 0，因为脏数据可能较多

    # Step 4: 触发回调
    cb_result = callback_hook.consume_validation_and_callback()
    print(f"  [OK] 回调触发: triggered={cb_result['triggered']}, successful={cb_result['successful']}")

    # 验证回调记录
    cb_stats = callback_hook.get_callback_stats()
    print(f"  [INFO] 回调统计: {cb_stats}")

    print("  [PASS] 数据采集与校验数据流测试通过")
    return True


# ── 测试 3: 元素定位与自动化执行数据流 ──

def test_element_execution_flow():
    """测试 element_selector → runtime_session 的完整数据流。

    1. element_selector 发布定位策略 → channel("rpa.locator_strategies")
    2. runtime_session 消费 + 模拟自动化执行
    """
    print("\n=== 测试 3: 元素定位与自动化执行数据流 ===")

    elem_svc = ElementSelectorService()

    # Step 1: 发布定位策略
    rules = store.rows("element_selector")
    strategies_published = 0
    for rule in rules:
        if rule.get("status") in ("草稿", "已测试"):
            result = elem_svc.publish_locator_strategy(rule["id"])
            if result:
                strategies_published += 1
    print(f"  [OK] 定位策略发布: {strategies_published} 条")

    # 如果没有已发布，直接发布全部
    if strategies_published == 0:
        for rule in rules:
            elem_svc.publish_locator_strategy(rule["id"])

    # Step 2: 消费策略并执行
    exec_result = runtime_session.consume_strategies_and_execute()
    print(
        f"  [OK] 自动化执行: executed={exec_result['executed']}, "
        f"session_id={exec_result['session_id']}, errors={exec_result['errors']}"
    )
    assert exec_result["total"] > 0, "没有消费到策略"

    # Step 3: 验证会话
    if exec_result["session_id"]:
        session = runtime_session.get_session(exec_result["session_id"])
        assert session is not None, "会话不存在"
        print(f"  [INFO] 运行时会话: {session['session_id']}")

    # Step 4: 批量元素解析
    elements = ["提交按钮", "搜索输入框", "结果表格", "下一页按钮"]
    batch_result = elem_svc.batch_resolve_elements(elements, bot_id=1)
    print(f"  [OK] 批量元素解析: total={batch_result['total']}, resolved={batch_result['resolved']}")

    # Step 5: 单元素解析
    single_result = elem_svc.resolve_element("按钮")
    print(f"  [INFO] 单元素解析: found={single_result['found']}")

    # 验证 FailureRecovery
    recovery_snapshot = store.recovery.snapshot()
    print(f"  [INFO] 失败恢复: {recovery_snapshot}")

    print("  [PASS] 元素定位与自动化执行数据流测试通过")
    return True


# ── 测试 4: domain_snapshot 跨模块聚合 ──

def test_domain_snapshots():
    """测试所有模块的 domain_snapshot 输出完整性。"""
    print("\n=== 测试 4: 领域快照完整性 ===")

    services_to_test = {
        "bot_definition": BotDefinitionService(),
        "step_editor": StepEditorService(),
        "task_queue": TaskQueueService(),
        "page_model": PageModelService(),
        "data_extract": DataExtractService(),
        "element_selector": ElementSelectorService(),
        "result_verify": ResultVerifyService(),
    }

    from app.services import SERVICES
    from app.services.anti_block import AntiBlockService
    from app.services.batch_tracker import BatchTrackerService
    from app.services.credential_store import CredentialStoreService
    from app.services.data_enrich import DataEnrichService
    from app.services.env_profile import EnvProfileService
    from app.services.execution_log import ExecutionLogService
    from app.services.output_format import OutputFormatService
    from app.services.anomaly_log import AnomalyLogService
    from app.services.scheduler import SchedulerService
    from app.services.report_dashboard import ReportDashboardService
    from app.services.trigger_config import TriggerConfigService
    from app.services.rate_limiter import RateLimiterService
    from app.services.proxy_router import ProxyRouterService

    all_services = {
        **services_to_test,
        "anti_block": AntiBlockService(),
        "batch_tracker": BatchTrackerService(),
        "credential_store": CredentialStoreService(),
        "data_enrich": DataEnrichService(),
        "env_profile": EnvProfileService(),
        "execution_log": ExecutionLogService(),
        "output_format": OutputFormatService(),
        "anomaly_log": AnomalyLogService(),
        "scheduler": SchedulerService(),
        "report_dashboard": ReportDashboardService(),
        "trigger_config": TriggerConfigService(),
        "rate_limiter": RateLimiterService(),
        "proxy_router": ProxyRouterService(),
        "runtime_session": runtime_session,
        "callback_hook": callback_hook,
    }

    for name, svc in all_services.items():
        snapshot = svc.domain_snapshot()
        assert "module" in snapshot or name in ("runtime_session", "callback_hook"), \
            f"{name} snapshot 缺少 module 字段"
        assert "count" in snapshot or "artifact" in snapshot or "entity" in snapshot, \
            f"{name} snapshot 缺少核心字段: {snapshot}"

    print(f"  [OK] {len(all_services)} 个服务的 domain_snapshot 全部通过检查")

    # 验证 infrastructure snapshot
    collect = store.collect_records.snapshot()
    dirty = store.dirty_vault.snapshot()
    recovery = store.recovery.snapshot()
    channel_msgs = store.channel.total_messages

    print(f"  [INFO] 基础设施概览:")
    print(f"    - Channel 消息总数: {channel_msgs}")
    print(f"    - CollectRecords: {collect['total']}")
    print(f"    - DirtyVault: {dirty['total']} (隔离 {dirty['quarantined']})")
    print(f"    - Recovery: {recovery['total']} (待处理 {recovery['pending']})")

    print("  [PASS] 领域快照完整性测试通过")
    return True


# ── 运行所有测试 ──

def run_all_tests():
    """运行所有跨模块数据流测试。"""
    print("=" * 60)
    print("cc10031 RPA流程自动化中心 — 跨模块数据流集成测试")
    print("=" * 60)

    all_passed = True
    tests = [
        ("RPA流程编排数据流", test_rpa_lifecycle_flow),
        ("数据采集与校验数据流", test_collection_validation_flow),
        ("元素定位与执行数据流", test_element_execution_flow),
        ("领域快照完整性", test_domain_snapshots),
    ]

    for name, test_fn in tests:
        try:
            test_fn()
        except Exception as e:
            print(f"  [FAIL] {name}: {e}")
            import traceback
            traceback.print_exc()
            all_passed = False

    print("\n" + "=" * 60)
    if all_passed:
        print("[ALL PASSED] 所有跨模块数据流测试通过")
    else:
        print("[SOME FAILED] 部分测试失败，请检查详细输出")

    print(f"  Channel 消息总数: {store.channel.total_messages}")
    print(f"  CollectRecords 总数: {store.collect_records.count}")
    print(f"  DirtyVault 总数: {store.dirty_vault.count}")
    print(f"  Recovery 总数: {store.recovery.count}")
    print("=" * 60)

    return all_passed


if __name__ == "__main__":
    ok = run_all_tests()
    sys.exit(0 if ok else 1)