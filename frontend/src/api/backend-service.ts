import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

/** 跨模块数据流：获取全平台领域快照 */
export async function loadNodeStats(module?: string): Promise<Record<string, unknown>> {
  const query = module ? `?module=${module}` : ''
  const response = await request(`/api/nodestats${query}`)
  if (!response.ok) throw new Error('节点统计读取失败')
  return await response.json()
}

/** 跨模块数据流：获取指定 topic 的通道消息 */
export async function loadChannelSnapshot(topic: string): Promise<Record<string, unknown>> {
  const response = await request(`/api/channel/${topic}`)
  if (!response.ok) throw new Error('通道快照读取失败')
  return await response.json()
}

/** 跨模块数据流：查询脏数据隔离区 */
export async function loadDirtyVault(source?: string): Promise<Record<string, unknown>> {
  const query = source ? `?source=${source}` : ''
  const response = await request(`/api/dirty-vault${query}`)
  if (!response.ok) throw new Error('脏数据查询失败')
  return await response.json()
}

/** 跨模块数据流：查询采集记录 */
export async function loadCollectRecords(batchRef?: string): Promise<Record<string, unknown>> {
  const query = batchRef ? `?batch_ref=${batchRef}` : ''
  const response = await request(`/api/collect-records${query}`)
  if (!response.ok) throw new Error('采集记录查询失败')
  return await response.json()
}

/** 跨模块数据流：执行RPA全生命周期管道 */
export async function triggerPipeline(lifecycle: string): Promise<Record<string, unknown>> {
  const response = await request(`/api/pipeline/${lifecycle}`, {
    method: 'POST',
  })
  if (!response.ok) throw new Error('管道执行失败')
  return await response.json()
}

/** 跨模块数据流：获取失败恢复状态 */
export async function loadRecoveryStats(): Promise<Record<string, unknown>> {
  const response = await request('/api/recovery')
  if (!response.ok) throw new Error('恢复状态查询失败')
  return await response.json()
}

// ---- 数据提取工作台：站点规则准入台账 ----

export type ExtractRuleRef = {
  rule_id: number | null
  rule_name: string | null
  rule_version: number | string | null
  rule_status?: string | null
  status?: string
  reason?: string
  source?: string | null
  fields?: string[]
}

export type ExtractSiteAdmission = {
  site: string
  admission: string
  rule_policy: string
  blocked_reason: string | null
  admitted_rules: ExtractRuleRef[]
  rejected_rules: ExtractRuleRef[]
  pending_source_configs: number
  models: { model_id: number; model_name: string; model_status: string; pending: boolean }[]
  detail: {
    version: number
    process_state: string
    batch_ref: string | null
    extracted: number
    dirty: number
    updated_at: string
    field_count: number
  } | null
}

export type FieldMappingEntry = {
  field: string
  hit: boolean
  rule_id: number | null
  rule_name: string | null
  rule_version: string | null
  rule_status: string | null
  source: string | null
  origin: string
  note: string | null
}

export type FieldMappingDetail = {
  site: string
  version: number
  process_state: string
  admission: string
  rule_policy: string
  blocked_reason: string | null
  batch_ref: string | null
  model_id: number | null
  source_snapshot: Record<string, unknown>
  field_mappings: FieldMappingEntry[]
  admitted_rules: ExtractRuleRef[]
  rejected_rules: ExtractRuleRef[]
  extracted: number
  dirty: number
  updated_at: string
}

/** 数据提取工作台：按站点查看准入状态、规则版本和阻塞原因 */
export async function loadExtractSites(): Promise<ExtractSiteAdmission[]> {
  const response = await request('/api/extract-workbench/sites')
  if (!response.ok) throw new Error('站点准入台账读取失败')
  const payload = await response.json()
  return payload.sites as ExtractSiteAdmission[]
}

/** 数据提取工作台：读取站点持久保存的字段映射采集明细 */
export async function loadExtractSiteDetail(site: string): Promise<FieldMappingDetail | null> {
  const response = await request(`/api/extract-workbench/sites/${encodeURIComponent(site)}/detail`)
  if (!response.ok) throw new Error('字段映射采集明细读取失败')
  const payload = await response.json()
  return (payload.detail ?? null) as FieldMappingDetail | null
}

/** 数据提取工作台：消费待处理页面源配置，执行规则准入与采集 */
export async function runExtractCollect(): Promise<Record<string, unknown>> {
  const response = await request('/api/extract-workbench/collect', { method: 'POST' })
  if (!response.ok) throw new Error('采集执行失败')
  return await response.json()
}

/** 数据提取工作台：重新评估站点准入（解除阻塞/冲突终态） */
export async function reevaluateExtractSite(site: string): Promise<Record<string, unknown>> {
  const response = await request('/api/extract-workbench/reevaluate', {
    method: 'POST',
    body: JSON.stringify({ site }),
  })
  if (!response.ok) throw new Error('重新评估失败')
  return await response.json()
}
