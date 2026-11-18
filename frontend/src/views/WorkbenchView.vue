<template>
  <section class="page" :data-module="meta.key">
    <header class="page-head">
      <div>
        <h2>{{ meta.name }}</h2>
        <p class="page-desc">{{ meta.desc }}</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">
          登记{{ meta.entity }}
        </button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">
          导出{{ meta.name }}清单
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p class="status-legend">
      <span v-for="item in statusSummary" :key="item.status" class="legend-item">
        {{ item.status }}：{{ item.count }}
      </span>
    </p>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in meta.fields" :key="column">{{ column }}</th>
          <th>当前状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in meta.fields" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row.status }}</td>
          <td class="row-actions">
            <button
              v-for="action in meta.actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="meta.fields.length + 2" class="empty-state">
            暂无{{ meta.name }}数据，可先登记{{ meta.entity }}
          </td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记{{ meta.entity }}</h3>
        <label v-for="field in meta.fields" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <section v-if="meta.key === 'data_extract'" class="extract-panel" data-testid="extract-admission-panel">
      <header class="panel-head">
        <div>
          <h3>站点规则准入台账</h3>
          <p class="panel-desc">
            仅「已生效」提取规则参与采集；无准入规则的站点保留阻塞状态，不静默降级。
            每个字段映射记录实际命中的规则版本，明细持久保存，重新进入页面仍是同一份。
          </p>
        </div>
        <div class="page-actions">
          <button class="btn primary" data-testid="extract-collect" type="button" @click="runCollect">
            执行采集（消费待处理源配置）
          </button>
          <button class="btn ghost" data-testid="extract-refresh" type="button" @click="reloadExtractSites">
            刷新准入状态
          </button>
        </div>
      </header>
      <table class="data-table">
        <thead>
          <tr>
            <th>站点</th>
            <th>准入状态</th>
            <th>命中规则版本</th>
            <th>阻塞原因</th>
            <th>待处理源配置</th>
            <th>明细版本</th>
            <th>处理状态</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="site in extractSites" :key="site.site" :data-testid="`extract-site-${site.site}`">
            <td>{{ site.site }}</td>
            <td>
              <span class="admission-tag" :data-state="site.admission">{{ site.admission }}</span>
            </td>
            <td>{{ formatRuleVersions(site) }}</td>
            <td>{{ site.blocked_reason ?? '—' }}</td>
            <td>{{ site.pending_source_configs }}</td>
            <td>{{ site.detail ? `v${site.detail.version}` : '—' }}</td>
            <td>{{ site.detail?.process_state ?? '待处理' }}</td>
            <td class="row-actions">
              <button class="link" type="button" :data-testid="`extract-detail-${site.site}`" @click="openExtractDetail(site.site)">
                采集明细
              </button>
              <button class="link" type="button" :data-testid="`extract-reevaluate-${site.site}`" @click="reevaluateSite(site.site)">
                重新评估
              </button>
            </td>
          </tr>
          <tr v-if="!extractSites.length">
            <td colspan="8" class="empty-state">暂无站点准入记录，可先发布页面模型源端配置</td>
          </tr>
        </tbody>
      </table>
    </section>

    <div v-if="showExtractDetail" class="modal-backdrop" data-testid="extract-detail-modal">
      <div class="modal extract-detail-modal">
        <h3>字段映射采集明细 — {{ extractDetail?.site }}</h3>
        <p class="detail-meta">
          准入：{{ extractDetail?.admission }} ｜ 准入策略：{{ extractDetail?.rule_policy }} ｜
          明细版本：v{{ extractDetail?.version }} ｜ 批次：{{ extractDetail?.batch_ref ?? '—' }} ｜
          处理状态：{{ extractDetail?.process_state }}
        </p>
        <p v-if="extractDetail?.blocked_reason" class="error-text">
          阻塞原因：{{ extractDetail.blocked_reason }}
        </p>
        <table class="data-table">
          <thead>
            <tr>
              <th>字段</th>
              <th>命中</th>
              <th>命中规则</th>
              <th>规则版本</th>
              <th>规则状态</th>
              <th>字段来源</th>
              <th>备注</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="mapping in extractDetail?.field_mappings ?? []" :key="mapping.field">
              <td>{{ mapping.field }}</td>
              <td>{{ mapping.hit ? '命中' : '未命中' }}</td>
              <td>{{ mapping.rule_name ?? '—' }}</td>
              <td>{{ mapping.rule_version ?? '—' }}</td>
              <td>{{ mapping.rule_status ?? '—' }}</td>
              <td>{{ mapping.origin }}</td>
              <td>{{ mapping.note ?? '—' }}</td>
            </tr>
          </tbody>
        </table>
        <div class="page-actions">
          <button class="btn" data-testid="extract-detail-close" type="button" @click="showExtractDetail = false">关闭</button>
        </div>
      </div>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条{{ meta.name }}记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  createEntry,
  downloadEntries,
  listEntries,
  loadExtractSiteDetail,
  loadExtractSites,
  moduleMeta,
  reevaluateExtractSite,
  runAction as applyAction,
  runExtractCollect,
} from '@/api/backend-service'
import type { EntryRow } from '@/data/types'
import type { ExtractSiteAdmission, FieldMappingDetail } from '@/api/backend-service'

const props = defineProps<{ moduleKey: string }>()
const meta = moduleMeta(props.moduleKey)
const rows = ref<EntryRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = meta.fields.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const extractSites = ref<ExtractSiteAdmission[]>([])
const extractDetail = ref<(FieldMappingDetail & { site: string }) | null>(null)
const showExtractDetail = ref(false)
const stats = computed(() => [
  { label: meta.metrics[0], value: total.value },
  { label: meta.metrics[1], value: rows.value.filter((row) => row.status === meta.statuses[1]).length },
  {
    label: meta.metrics[2],
    value: rows.value.filter((row) => row.status === meta.statuses[meta.statuses.length - 1]).length,
  },
])
const statusSummary = computed(() =>
  meta.statuses.map((status) => ({
    status,
    count: rows.value.filter((row) => String(row.status) === status).length,
  })),
)

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  try {
    await downloadEntries(meta.key, filters.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(meta.fields.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const result = await createEntry(meta.key, createValues.value)
    if (!result.ok) throw new Error(result.message)
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: EntryRow) {
  errorMessage.value = ''
  try {
    const result = await applyAction(meta.key, Number(row.id), action)
    if (!result.ok) throw new Error(result.message)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  try {
    const payload = await listEntries(meta.key, filters.value)
    rows.value = payload.items
    total.value = payload.total
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '业务列表读取失败'
  }
}

function formatRuleVersions(site: ExtractSiteAdmission): string {
  if (!site.admitted_rules.length) return '—'
  return site.admitted_rules
    .map((rule) => `${rule.rule_name ?? rule.rule_id} v${rule.rule_version}`)
    .join('、')
}

async function reloadExtractSites() {
  if (meta.key !== 'data_extract') return
  try {
    extractSites.value = await loadExtractSites()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '站点准入状态读取失败'
  }
}

async function openExtractDetail(site: string) {
  errorMessage.value = ''
  try {
    const detail = await loadExtractSiteDetail(site)
    if (!detail) {
      errorMessage.value = `站点 ${site} 尚无字段映射采集明细，等待源配置处理`
      return
    }
    extractDetail.value = { ...detail, site }
    showExtractDetail.value = true
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集明细读取失败'
  }
}

async function reevaluateSite(site: string) {
  errorMessage.value = ''
  try {
    const result = await reevaluateExtractSite(site)
    if (!result.ok) throw new Error('重新评估未通过')
    await reloadExtractSites()
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '重新评估失败'
  }
}

async function runCollect() {
  errorMessage.value = ''
  try {
    await runExtractCollect()
    await reloadExtractSites()
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集执行失败'
  }
}

onMounted(() => {
  void reload()
  void reloadExtractSites()
})
</script>

<style scoped>
.extract-panel {
  margin-top: 18px;
  border-top: 1px dashed var(--border);
  padding-top: 14px;
}

.panel-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
  margin-bottom: 10px;
}

.panel-head h3 {
  margin: 0 0 4px;
  font-size: 15px;
}

.panel-desc {
  margin: 0;
  color: var(--muted);
  font-size: 12px;
  max-width: 640px;
}

.admission-tag {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 12px;
  background: #eef2ff;
  color: #3730a3;
}

.admission-tag[data-state='已阻塞'] {
  background: #fef2f2;
  color: #b42318;
}

.admission-tag[data-state='已准入(历史版本)'] {
  background: #fff7ed;
  color: #9a3412;
}

.extract-detail-modal {
  width: min(860px, 100%);
}

.detail-meta {
  color: var(--muted);
  font-size: 12px;
}
</style>
