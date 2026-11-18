"""数据提取领域服务 — 消费源端配置 + 字段映射，执行数据采集。

数据工程核心：数据归档存储与快照管理 — 跨模块数据产物

数据流:
    channel("rpa.source_configured") ← page_model
    → data_extract 消费 → 规则准入 → 执行采集 → CollectRecord + DirtyDataVault
    → 字段映射采集明细写入 FieldMappingLedger（版本受控）
    → channel("rpa.data_collected") → result_verify

规则准入约束（本模块核心修复）：
- 只有「已生效」提取规则允许参与采集；草稿/已测试/已过期规则一律
  被准入层拒绝，不再被兜底逻辑拼接进采集字段列表。
- 每个字段映射必须记录实际命中的已生效规则版本，写入字段映射采集明细。
- 站点没有准入规则时保留阻塞状态（暂停新批次等待规则启用），
  不静默降级；若历史批次仍可读取（存在上一已生效版本快照），
  可回退到上一已生效版本继续采集，两种方案都禁止草稿规则进入新明细。
- 明细台账按站点版本受控写入，并发再次写入只接受一个版本；
  冲突会把页面源配置的处理状态推进终态「冲突终止」。
"""
from __future__ import annotations

import random
import re
from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

#: 允许参与采集的规则状态
EFFECTIVE_STATUS = "已生效"

#: 准入策略
POLICY_CURRENT = "current"            # 使用当前已生效规则
POLICY_LAST_EFFECTIVE = "last_effective"  # 回退上一已生效版本
POLICY_NONE = "none"                  # 无准入规则（阻塞）

#: 准入结论
ADMISSION_GRANTED = "已准入"
ADMISSION_GRANTED_HISTORICAL = "已准入(历史版本)"
ADMISSION_BLOCKED = "已阻塞"


class DataExtractService(BaseModuleService):
    MODULE = "data_extract"
    ENTITY = "提取规则"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["规则名称", "源页面结构", "提取字段", "提取路径", "数据类型", "格式化", "去空策略"]
    REQUIRED_FIELDS = ["规则名称", "源页面结构", "提取字段"]
    STATUS_ORDER = ["草稿", "已测试", "已生效", "已过期"]
    ACTION_RULES = {"测试提取": "草稿", "启用规则": "已测试"}
    NEGATIVE_ACTIONS = []

    # ---- 规则版本：登记/流转时维护版本号与已生效版本档案 ----

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        entry, missing = super().create_entry(values)
        if entry is not None:
            entry["rule_version"] = 1
        return entry, missing

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry, message = super().run_action(entry_id, action)
        if entry is not None:
            # 每次状态流转都推进规则版本；进入已生效时归档版本快照，
            # 供站点后续只剩草稿规则时回退到上一已生效版本。
            entry["rule_version"] = self._rule_version(entry) + 1
            if entry.get("status") == EFFECTIVE_STATUS:
                self._archive_effective_version(entry)
        return entry, message

    @staticmethod
    def _rule_version(rule: dict[str, Any]) -> int:
        try:
            return int(rule.get("rule_version") or 1)
        except (TypeError, ValueError):
            return 1

    @staticmethod
    def _rule_matches_site(rule: dict[str, Any], site: str) -> bool:
        """既有站点匹配口径：源页面结构包含站点前缀或全等。"""
        structure = str(rule.get("源页面结构") or "")
        if not site:
            return True
        if not structure:
            return False
        return site[:2] in structure or structure == site

    @staticmethod
    def _rule_fields(rule: dict[str, Any]) -> list[str]:
        """解析规则覆盖的提取字段（支持顿号/逗号/分号分隔的多字段口径）。"""
        raw = str(rule.get("提取字段") or "")
        parts = re.split(r"[、,，;；/]", raw)
        return [part.strip() for part in parts if part.strip()]

    def _archive_effective_version(self, rule: dict[str, Any]) -> None:
        """把当前已生效规则写入站点版本档案（上一已生效版本快照）。"""
        site_key = str(rule.get("源页面结构") or "")
        if not site_key:
            return
        store.rule_archive.put(site_key, {
            "site_key": site_key,
            "rule_id": rule.get("id"),
            "rule_name": rule.get("规则名称"),
            "rule_version": self._rule_version(rule),
            "extract_fields": self._rule_fields(rule),
            "extract_path": rule.get("提取路径", ""),
            "data_type": rule.get("数据类型", ""),
            "fmt": rule.get("格式化", ""),
            "null_policy": rule.get("去空策略", ""),
            "snapshotted_at": datetime.now(timezone.utc).isoformat(),
        })

    # ---- 规则准入 ----

    def _get_active_rules_for_site(self, site: str) -> list[dict[str, Any]]:
        """获取目标站点关联的已生效提取规则（field_mapping 语义）。

        准入修复：只返回「已生效」规则。草稿/已测试/已过期规则不再
        被兜底拼接；调用方需要兜底语义时必须走 resolve_rule_admission，
        由准入层显式给出阻塞或上一已生效版本。
        """
        rows = store.rows(self.MODULE)
        active = [r for r in rows if r.get("status") == EFFECTIVE_STATUS]
        if site:
            active = [r for r in active if self._rule_matches_site(r, site)]
        return active

    def resolve_rule_admission(self, site: str) -> dict[str, Any]:
        """解析站点规则准入：读取规则状态，给出准入结论与命中规则版本。

        Returns:
            {
              "site": str,
              "admission": 已准入 | 已准入(历史版本) | 已阻塞,
              "policy": current | last_effective | none,
              "admitted_rules": [{rule_id, rule_name, rule_version, fields, source}],
              "rejected_rules": [{rule_id, rule_name, status, reason}],
              "blocked_reason": str | None,
            }
        """
        rows = store.rows(self.MODULE)
        site_rules = [r for r in rows if self._rule_matches_site(r, site)]

        admitted: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        for rule in site_rules:
            if rule.get("status") == EFFECTIVE_STATUS:
                # 刷新上一已生效版本档案，保证历史版本回退可用
                self._archive_effective_version(rule)
                admitted.append({
                    "rule_id": rule.get("id"),
                    "rule_name": rule.get("规则名称"),
                    "rule_version": self._rule_version(rule),
                    "rule_status": EFFECTIVE_STATUS,
                    "fields": self._rule_fields(rule),
                    "source": POLICY_CURRENT,
                })
            else:
                rejected.append({
                    "rule_id": rule.get("id"),
                    "rule_name": rule.get("规则名称"),
                    "status": rule.get("status"),
                    "reason": f"规则状态为{rule.get('status')}，未生效规则不参与采集",
                })

        if admitted:
            return {
                "site": site,
                "admission": ADMISSION_GRANTED,
                "policy": POLICY_CURRENT,
                "admitted_rules": admitted,
                "rejected_rules": rejected,
                "blocked_reason": None,
            }

        # 站点没有已生效规则：历史批次仍可读取时允许回退到上一已生效
        # 版本继续采集；草稿/已测试规则同样被禁止进入新明细。
        archived = [
            snap for snap in store.rule_archive.all()
            if self._rule_matches_site(
                {"源页面结构": snap.get("site_key", "")}, site)
        ]
        if archived:
            return {
                "site": site,
                "admission": ADMISSION_GRANTED_HISTORICAL,
                "policy": POLICY_LAST_EFFECTIVE,
                "admitted_rules": [
                    {
                        "rule_id": snap.get("rule_id"),
                        "rule_name": snap.get("rule_name"),
                        "rule_version": int(snap.get("rule_version") or 1),
                        "rule_status": "已生效(历史版本)",
                        "fields": list(snap.get("extract_fields") or []),
                        "source": POLICY_LAST_EFFECTIVE,
                    }
                    for snap in archived
                ],
                "rejected_rules": rejected,
                "blocked_reason": None,
            }

        # 无准入规则：保留阻塞状态，暂停新批次等待规则启用，不静默降级。
        if rejected:
            statuses = "、".join(sorted({r["status"] for r in rejected}))
            reason = f"站点仅存在{statuses}规则，已暂停新批次等待规则启用"
        else:
            reason = "站点无匹配提取规则，已暂停新批次等待规则登记"
        return {
            "site": site,
            "admission": ADMISSION_BLOCKED,
            "policy": POLICY_NONE,
            "admitted_rules": [],
            "rejected_rules": rejected,
            "blocked_reason": reason,
        }

    # ---- 字段映射采集明细 ----

    def build_field_mapping_detail(
        self,
        source_config: dict[str, Any],
        admission: dict[str, Any],
        batch_ref: str | None,
        *,
        msg_id: int | None = None,
        extracted: int = 0,
        dirty: int = 0,
    ) -> dict[str, Any]:
        """按准入结论解析每个字段实际命中的规则版本，生成采集明细。

        只有准入规则（当前已生效或上一已生效版本）能把字段拼进采集
        列表；页面模型推断字段未命中准入规则时如实记录 miss，不降级
        到草稿规则。
        """
        model_fields = list(source_config.get("fields_to_extract") or [])
        admitted = admission.get("admitted_rules", [])

        mappings: list[dict[str, Any]] = []
        seen: set[str] = set()

        def _hit_rule(field: str) -> dict[str, Any] | None:
            for rule in admitted:
                if field in (rule.get("fields") or []):
                    return rule
            return None

        for field in model_fields:
            rule = _hit_rule(field)
            seen.add(field)
            mappings.append(self._mapping_entry(field, rule, origin="页面模型"))

        # 准入规则补充的字段（旧兜底逻辑拼草稿规则字段的位置，
        # 现在只允许准入规则进入字段列表）
        for rule in admitted:
            for field in rule.get("fields") or []:
                if field and field not in seen:
                    seen.add(field)
                    mappings.append(self._mapping_entry(field, rule, origin="提取规则"))

        blocked = admission.get("admission") == ADMISSION_BLOCKED
        return {
            "model_id": source_config.get("model_id"),
            "msg_id": msg_id,
            "batch_ref": batch_ref,
            "process_state": "已阻塞" if blocked else "已准入",
            "admission": admission.get("admission"),
            "rule_policy": admission.get("policy"),
            "blocked_reason": admission.get("blocked_reason"),
            "source_snapshot": {
                "model_id": source_config.get("model_id"),
                "model_name": source_config.get("model_name"),
                "model_version": source_config.get("version"),
                "pagination_mode": source_config.get("pagination_mode"),
                "fields_to_extract": model_fields,
            },
            "field_mappings": mappings,
            "admitted_rules": [
                {k: rule[k] for k in ("rule_id", "rule_name", "rule_version", "rule_status", "source")}
                for rule in admitted
            ],
            "rejected_rules": admission.get("rejected_rules", []),
            "extracted": extracted,
            "dirty": dirty,
        }

    @staticmethod
    def _mapping_entry(
        field: str,
        rule: dict[str, Any] | None,
        *,
        origin: str,
    ) -> dict[str, Any]:
        if rule is None:
            return {
                "field": field,
                "hit": False,
                "rule_id": None,
                "rule_name": None,
                "rule_version": None,
                "rule_status": None,
                "source": None,
                "origin": origin,
                "note": "页面模型推断字段，未命中已生效提取规则",
            }
        return {
            "field": field,
            "hit": True,
            "rule_id": rule.get("rule_id"),
            "rule_name": rule.get("rule_name"),
            "rule_version": f"v{rule.get('rule_version')}",
            "rule_status": rule.get("rule_status"),
            "source": rule.get("source"),
            "origin": origin,
            "note": None,
        }

    def _persist_detail(
        self,
        site: str,
        detail: dict[str, Any],
        expected_version: int | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """把字段映射采集明细写入台账（版本受控，并发只接受一个版本）。

        expected_version 为空时读取台账当前版本；调用方可显式传入
        已持有的版本号，过期版本会被台账拒绝并推进处理终态。
        """
        if expected_version is None:
            current = store.field_mapping_ledger.get(site)
            expected_version = current.get("version") if current else 0
        return store.field_mapping_ledger.upsert(site, detail, expected_version)

    def _apply_conflict_terminal(self, site: str, model_id: int | None) -> None:
        """写入冲突：页面源配置处理状态推进终态，并同步到页面建模业务面。

        台账侧已在 upsert 冲突时置为「冲突终止」；这里把页面建模中的
        源配置同步到其状态机终态（结构失效），保证跨业务面可见。
        """
        rows = store.rows("page_model")
        targets = [r for r in rows if model_id is not None and r.get("id") == model_id]
        if not targets:
            targets = [r for r in rows if r.get("目标站点") == site]
        for row in targets:
            row["status"] = "结构失效"
            row["abnormal"] = True
            row["pending"] = False

    def _sync_page_model_todo(
        self,
        source_config: dict[str, Any],
        *,
        blocked: bool,
    ) -> None:
        """跨业务面同步待办：阻塞的源配置在页面建模侧保持待处理。"""
        model_id = source_config.get("model_id")
        site = source_config.get("target_site")
        rows = store.rows("page_model")
        targets = [r for r in rows if model_id is not None and r.get("id") == model_id]
        if not targets and site:
            targets = [r for r in rows if r.get("目标站点") == site]
        for row in targets:
            row["pending"] = blocked

    # ---- 跨模块业务方法 (field_mapping + data_extractor 角色) ----

    def consume_source_and_extract(self) -> dict[str, Any]:
        """消费 page_model 发布的源端配置，执行规则准入与数据采集。

        从 channel("rpa.source_configured") 载入待处理页面源配置快照，
        按准入结论采集并把实际规则版本写入字段映射采集明细：
        - 已准入：仅准入规则参与字段拼接，明细记录每字段命中的规则版本；
        - 已阻塞：不采集、不降级，明细保留阻塞状态与阻塞原因。

        Returns:
            {"extracted": int, "dirty": int, "blocked": int, "batches": [...]}
        """
        msgs = store.channel.consume(
            consumer=self.MODULE,
            topic="rpa.source_configured",
        )
        if not msgs:
            return {"extracted": 0, "dirty": 0, "blocked": 0, "batches": []}

        batches = []
        for msg in msgs:
            source_config = msg.payload
            site = source_config.get("target_site", "")
            batch_ref = f"BATC-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{msg.msg_id}"

            # 1. 规则准入：读取源端配置涉及站点的规则状态
            admission = self.resolve_rule_admission(site)

            if admission["admission"] == ADMISSION_BLOCKED:
                # 2a. 无准入规则：保留阻塞状态，不静默降级
                detail = self.build_field_mapping_detail(
                    source_config, admission, batch_ref, msg_id=msg.msg_id)
                outcome, record = self._persist_detail(site, detail)
                if outcome in ("conflict", "terminated"):
                    self._apply_conflict_terminal(site, source_config.get("model_id"))
                self._sync_page_model_todo(source_config, blocked=True)

                store.channel.publish(
                    topic="rpa.data_collected",
                    source=self.MODULE,
                    payload={
                        "batch_ref": batch_ref,
                        "model_id": source_config.get("model_id"),
                        "site": site,
                        "blocked": True,
                        "blocked_reason": admission["blocked_reason"],
                        "admission": admission["admission"],
                        "extracted": 0,
                        "dirty": 0,
                        "extract_rules_applied": 0,
                        "detail_version": record.get("version"),
                    },
                )
                store.channel.confirm(msg.msg_id, self.MODULE)
                batches.append({
                    "batch_ref": batch_ref,
                    "site": site,
                    "blocked": True,
                    "reason": admission["blocked_reason"],
                    "extracted": 0,
                    "dirty": 0,
                })
                continue

            # 2b. 已准入：只拼接准入规则字段，模拟采集
            detail = self.build_field_mapping_detail(
                source_config, admission, batch_ref, msg_id=msg.msg_id)
            fields_to_extract = [m["field"] for m in detail["field_mappings"]]
            extracted, dirty = self._simulate_collection(
                source_config, fields_to_extract, batch_ref)

            # 3. 字段映射采集明细：写入实际命中的规则版本
            detail["extracted"] = len(extracted)
            detail["dirty"] = len(dirty)
            outcome, record = self._persist_detail(site, detail)
            if outcome in ("conflict", "terminated"):
                self._apply_conflict_terminal(site, source_config.get("model_id"))
            self._sync_page_model_todo(source_config, blocked=False)

            # 4. 写 channel
            result_payload = {
                "batch_ref": batch_ref,
                "model_id": source_config.get("model_id"),
                "site": site,
                "blocked": False,
                "admission": admission["admission"],
                "rule_policy": admission["policy"],
                "extracted": len(extracted),
                "dirty": len(dirty),
                "extract_rules_applied": len(admission["admitted_rules"]),
                "rule_versions": {
                    str(r["rule_id"]): f"v{r['rule_version']}"
                    for r in admission["admitted_rules"]
                },
                "detail_version": record.get("version"),
                "sample_records": extracted[:3],
            }
            store.channel.publish(
                topic="rpa.data_collected",
                source=self.MODULE,
                payload=result_payload,
            )

            store.channel.confirm(msg.msg_id, self.MODULE)
            batches.append({
                "batch_ref": batch_ref,
                "site": site,
                "blocked": False,
                "admission": admission["admission"],
                "rule_policy": admission["policy"],
                "extracted": len(extracted),
                "dirty": len(dirty),
            })

        return {
            "extracted": sum(b["extracted"] for b in batches),
            "dirty": sum(b["dirty"] for b in batches),
            "blocked": sum(1 for b in batches if b.get("blocked")),
            "batches": batches,
        }

    def _simulate_collection(
        self,
        source_config: dict[str, Any],
        fields_to_extract: list[str],
        batch_ref: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """模拟从目标站点采集数据。

        字段列表由准入层给出（页面模型字段 + 准入规则字段），
        部分记录会因字段缺失被标记为脏数据。

        Returns:
            (valid_records, dirty_records)
        """
        fields_to_extract = list(fields_to_extract)

        # 模拟采集 8-15 条记录
        record_count = random.randint(8, 15)
        valid_records = []
        dirty_records = []

        for i in range(record_count):
            record = self._generate_mock_record(source_config, fields_to_extract, i)

            # 质量检查：模拟约 15% 的脏数据
            if random.random() < 0.15:
                # 随机缺失字段或格式异常
                reasons = ["字段缺失", "格式非法", "解析超时", "编码错误"]
                reason = random.choice(reasons)
                if reason == "字段缺失":
                    del record[random.choice(list(record.keys()))]

                store.dirty_vault.quarantine(
                    source=self.MODULE,
                    batch_ref=batch_ref,
                    raw_data=dict(record),
                    reason=reason,
                )
                dirty_records.append(record)
            else:
                # 正常数据：写入采集记录
                store.collect_records.add(
                    batch_ref=batch_ref,
                    source=self.MODULE,
                    fields=record,
                )
                valid_records.append(record)

        return valid_records, dirty_records

    def _generate_mock_record(
        self,
        config: dict[str, Any],
        fields: list[str],
        index: int,
    ) -> dict[str, Any]:
        """生成模拟采集记录（基于真实的数据工程场景）。"""
        site = config.get("target_site", "unknown")
        record: dict[str, Any] = {"_seq": index + 1}

        sample_templates = {
            "序号": lambda i: i + 1,
            "名称": lambda i: f"{site}-产品-{1000 + i}",
            "标题": lambda i: f"关于{site}的第{i + 1}期报告",
            "状态": lambda i: random.choice(["正常", "异常", "待审核"]),
            "金额": lambda i: round(random.uniform(100, 99999), 2),
            "日期": lambda i: f"2026-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}",
            "链接": lambda i: f"https://{site}.example.com/item/{i + 1}",
            "摘要": lambda i: f"{site}数据采集摘要 #{i + 1}",
            "时间": lambda i: datetime.now(timezone.utc).isoformat(),
            "字段名": lambda i: f"field_{i + 1}",
            "字段值": lambda i: f"value_{random.randint(1, 100)}",
            "内容": lambda i: f"{site}采集内容行 {i + 1}",
            "标签": lambda i: random.choice(["采集", "RPA", "自动化"]),
            "发布日期": lambda i: f"2026-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}",
            "类型": lambda i: random.choice(["文本", "数值", "日期"]),
            "必填": lambda i: random.choice(["是", "否"]),
            "数据项1": lambda i: f"data1_{i}",
            "数据项2": lambda i: f"data2_{i}",
            "数据项3": lambda i: f"data3_{i}",
        }

        for field in fields:
            if field in sample_templates:
                record[field] = sample_templates[field](index)
            else:
                record[field] = f"{field}-{index + 1}"

        return record

    def execute_mapping_and_extract(
        self,
        model_id: int,
        target_fields: list[str] | None = None,
    ) -> dict[str, Any]:
        """直接执行字段映射 + 数据提取（不通过 channel 的简化版）。

        用于直接调用场景：已知 page_model 和规则，立即执行采集。
        同样走规则准入：无准入规则的站点返回阻塞结果而不采集。

        Args:
            model_id: 页面模型 ID
            target_fields: 额外目标字段（可选）

        Returns:
            采集结果概要（含准入结论与明细版本）
        """
        model_entry = store.find("page_model", model_id)
        if model_entry is None:
            return {"error": f"页面模型 {model_id} 不存在"}

        source_config = {
            "model_id": model_id,
            "target_site": model_entry.get("目标站点", "unknown"),
            "fields_to_extract": target_fields or ["名称", "状态", "金额", "日期"],
        }
        site = source_config["target_site"]
        admission = self.resolve_rule_admission(site)
        batch_ref = f"DIRECT-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"

        if admission["admission"] == ADMISSION_BLOCKED:
            detail = self.build_field_mapping_detail(
                source_config, admission, batch_ref)
            outcome, record = self._persist_detail(site, detail)
            if outcome in ("conflict", "terminated"):
                self._apply_conflict_terminal(site, model_id)
            self._sync_page_model_todo(source_config, blocked=True)
            return {
                "batch_ref": batch_ref,
                "model_id": model_id,
                "blocked": True,
                "blocked_reason": admission["blocked_reason"],
                "extracted": 0,
                "dirty": 0,
                "detail_version": record.get("version"),
            }

        detail = self.build_field_mapping_detail(
            source_config, admission, batch_ref)
        fields = [m["field"] for m in detail["field_mappings"]]
        valid, dirty = self._simulate_collection(source_config, fields, batch_ref)
        detail["extracted"] = len(valid)
        detail["dirty"] = len(dirty)
        outcome, record = self._persist_detail(site, detail)
        if outcome in ("conflict", "terminated"):
            self._apply_conflict_terminal(site, model_id)
        self._sync_page_model_todo(source_config, blocked=False)

        return {
            "batch_ref": batch_ref,
            "model_id": model_id,
            "blocked": False,
            "admission": admission["admission"],
            "rule_policy": admission["policy"],
            "extracted": len(valid),
            "dirty": len(dirty),
            "detail_version": record.get("version"),
            "sample": valid[:3],
        }

    # ---- 数据提取工作台：按站点查看准入状态 / 规则版本 / 阻塞原因 ----

    def site_admission_overview(self) -> list[dict[str, Any]]:
        """汇总站点维度准入视图：业务清单与结果台账同源一致。

        站点来源：页面模型目标站点 ∪ 台账已记录站点 ∪ 待处理源配置站点。
        每个站点给出实时准入结论（规则状态、规则版本、阻塞原因）和
        台账中持久保存的字段映射采集明细版本。
        """
        sites: dict[str, dict[str, Any]] = {}

        for row in store.rows("page_model"):
            site = str(row.get("目标站点") or "").strip()
            if site:
                sites.setdefault(site, {"models": []})
                sites[site]["models"].append({
                    "model_id": row.get("id"),
                    "model_name": row.get("模型名称"),
                    "model_status": row.get("status"),
                    "pending": bool(row.get("pending")),
                })

        pending_configs: dict[str, int] = {}
        for msg in store.channel.snapshot(topic="rpa.source_configured"):
            if self.MODULE in msg.get("consumed_by", []):
                continue
            site = str(msg.get("payload", {}).get("target_site") or "").strip()
            if site:
                pending_configs[site] = pending_configs.get(site, 0) + 1
                sites.setdefault(site, {"models": []})

        for record in store.field_mapping_ledger.list():
            site = str(record.get("site") or "").strip()
            if site:
                sites.setdefault(site, {"models": []})

        overview: list[dict[str, Any]] = []
        for site in sorted(sites):
            admission = self.resolve_rule_admission(site)
            record = store.field_mapping_ledger.get(site)
            overview.append({
                "site": site,
                "admission": admission["admission"],
                "rule_policy": admission["policy"],
                "blocked_reason": admission["blocked_reason"],
                "admitted_rules": admission["admitted_rules"],
                "rejected_rules": admission["rejected_rules"],
                "pending_source_configs": pending_configs.get(site, 0),
                "models": sites[site]["models"],
                "detail": {
                    "version": record.get("version"),
                    "process_state": record.get("process_state"),
                    "batch_ref": record.get("batch_ref"),
                    "extracted": record.get("extracted"),
                    "dirty": record.get("dirty"),
                    "updated_at": record.get("updated_at"),
                    "field_count": len(record.get("field_mappings", [])),
                } if record else None,
            })
        return overview

    def get_site_detail(self, site: str) -> dict[str, Any] | None:
        """读取站点持久保存的字段映射采集明细（重新进入页面仍是同一份）。"""
        return store.field_mapping_ledger.get(site)

    def reevaluate_site(self, site: str) -> dict[str, Any]:
        """重新评估站点准入：解除冲突终态，按当前规则状态重建明细。

        用于规则启用后解除阻塞，或冲突终态后的显式重新处理。
        不触发采集，只刷新准入结论与字段映射明细。
        """
        store.field_mapping_ledger.reset_terminal(site)
        admission = self.resolve_rule_admission(site)
        source_config = {"model_id": None, "target_site": site,
                         "fields_to_extract": []}
        current = store.field_mapping_ledger.get(site)
        if current and current.get("source_snapshot"):
            source_config = {
                "model_id": current.get("model_id"),
                "target_site": site,
                "fields_to_extract": current["source_snapshot"].get("fields_to_extract") or [],
                "model_name": current["source_snapshot"].get("model_name"),
                "version": current["source_snapshot"].get("model_version"),
                "pagination_mode": current["source_snapshot"].get("pagination_mode"),
            }
        detail = self.build_field_mapping_detail(
            source_config, admission,
            current.get("batch_ref") if current else None,
            extracted=current.get("extracted", 0) if current else 0,
            dirty=current.get("dirty", 0) if current else 0,
        )
        outcome, record = self._persist_detail(site, detail)
        if outcome in ("conflict", "terminated"):
            self._apply_conflict_terminal(site, source_config.get("model_id"))
        self._sync_page_model_todo(
            source_config, blocked=admission["admission"] == ADMISSION_BLOCKED)
        return {
            "site": site,
            "outcome": outcome,
            "admission": admission["admission"],
            "blocked_reason": admission["blocked_reason"],
            "detail": record,
        }

    def get_collection_stats(self) -> dict[str, Any]:
        """获取采集统计概览。"""
        collect_snapshot = store.collect_records.snapshot()
        dirty_snapshot = store.dirty_vault.snapshot()
        return {
            "collect_records": collect_snapshot,
            "dirty_vault": dirty_snapshot,
            "collection_events": len(
                store.channel.snapshot(topic="rpa.data_collected")),
            "field_mapping_ledger": store.field_mapping_ledger.snapshot(),
            "rule_archive": store.rule_archive.snapshot(),
        }

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        stats = self.get_collection_stats()
        return {**base, **stats}
