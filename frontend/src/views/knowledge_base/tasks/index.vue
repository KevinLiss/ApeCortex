<template>
  <div class="kb-tasks-page">
    <div class="page-header">
      <div>
        <h2>{{ t('knowledge.tasks.title') }}</h2>
        <p class="text-muted">{{ t('knowledge.tasks.subtitle') }}</p>
      </div>
      <div class="header-actions">
        <el-switch v-model="autoRefresh" :active-text="t('knowledge.tasks.autoRefresh')" size="small" />
        <el-button size="small" @click="fetchTasks">{{ t('common.action.refresh') }}</el-button>
      </div>
    </div>

    <el-card shadow="never" class="panel">
      <el-table :data="items" v-loading="loading" stripe>
        <el-table-column :label="t('knowledge.tasks.colType')" width="130">
          <template #default="{ row }">
            <el-tag size="small" effect="plain" round>{{ typeLabel(row.job_type) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="t('knowledge.tasks.colStatus')" width="100" align="center">
          <template #default="{ row }">
            <el-tag size="small" :type="statusTag(row.status)" effect="light" round>{{ statusLabel(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="t('knowledge.tasks.colProgress')" width="150">
          <template #default="{ row }">
            <el-progress :percentage="row.progress" :stroke-width="8" :status="row.status === 'failed' ? 'exception' : undefined" />
          </template>
        </el-table-column>
        <el-table-column :label="t('knowledge.tasks.colLlmCalls')" width="90" align="right">
          <template #default="{ row }">
            <span class="num">{{ row.llm_calls }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('knowledge.tasks.colResult')" min-width="200">
          <template #default="{ row }">
            <span v-if="row.error" class="err-text">{{ row.error }}</span>
            <span v-else class="result-text">{{ resultText(row) }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('knowledge.tasks.colTime')" width="160">
          <template #default="{ row }">
            <span class="time-text">{{ formatTime(row.created_at) }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('knowledge.tasks.colActions')" width="90" fixed="right" align="center">
          <template #default="{ row }">
            <el-button v-if="row.status === 'failed'" link size="small" type="primary"
              @click="handleRetry(row)" v-permission="'knowledge_base:task:retry'">
              {{ t('knowledge.tasks.retry') }}
            </el-button>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty :description="t('knowledge.tasks.empty')" :image-size="72" />
        </template>
      </el-table>

      <div class="pagination" v-if="total > pageSize">
        <el-pagination v-model:current-page="pageNum" :total="total" :page-size="pageSize"
          layout="total, prev, pager, next" small background @current-change="fetchTasks" />
      </div>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage } from 'element-plus'

import { getKbJobs, retryKbJob } from '@/api'

const { t } = useI18n()

const items = ref<any[]>([])
const total = ref(0)
const pageNum = ref(1)
const pageSize = 20
const loading = ref(false)
const autoRefresh = ref(true)
let timer: any = null

function statusTag(s: string): string {
  return { pending: 'info', running: 'primary', done: 'success', failed: 'danger', cancelled: 'warning' }[s] ?? 'info'
}
function statusLabel(s: string): string {
  const map: Record<string, string> = {
    pending: t('knowledge.tasks.statusPending'), running: t('knowledge.tasks.statusRunning'),
    done: t('knowledge.tasks.statusDone'), failed: t('knowledge.tasks.statusFailed'),
    cancelled: t('knowledge.tasks.statusCancelled'),
  }
  return map[s] ?? s
}
function typeLabel(j: string): string {
  const map: Record<string, string> = {
    wiki_ingest: t('knowledge.tasks.typeWikiIngest'),
    memory_extract: t('knowledge.tasks.typeMemoryExtract'),
    research: t('knowledge.tasks.typeResearch'),
    collect: t('knowledge.tasks.typeCollect'),
  }
  return map[j] ?? j
}

function resultText(row: any): string {
  const r = row.result || {}
  if (r.pages_new !== undefined) {
    return `new ${r.pages_new} / merged ${r.pages_merged} / rejected ${r.pages_rejected} · LLM ${r.llm_calls ?? row.llm_calls}`
  }
  if (r.inserted !== undefined) return `memories +${r.inserted}`
  return JSON.stringify(r).slice(0, 80)
}

function formatTime(iso: string | null): string {
  if (!iso) return '-'
  return iso.replace('T', ' ').slice(0, 19)
}

async function fetchTasks() {
  loading.value = true
  try {
    const res: any = await getKbJobs({ page: pageNum.value, page_size: pageSize })
    items.value = res?.items ?? []
    total.value = res?.total ?? 0
  } finally {
    loading.value = false
  }
}

async function handleRetry(row: any) {
  try {
    await retryKbJob(row.id)
    ElMessage.success(t('knowledge.tasks.retried'))
    fetchTasks()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.msg || e?.message || 'Error')
  }
}

onMounted(() => {
  fetchTasks()
  timer = setInterval(() => {
    if (autoRefresh.value && items.value.some((i) => i.status === 'running' || i.status === 'pending')) {
      fetchTasks()
    }
  }, 5000)
})

onBeforeUnmount(() => {
  if (timer) clearInterval(timer)
})
</script>

<style scoped>
.page-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  margin-bottom: 16px;
}

.page-header h2 {
  margin: 0 0 4px;
  font-size: 20px;
}

.header-actions {
  display: flex;
  align-items: center;
  gap: 12px;
}

.panel {
  border-radius: 10px;
}

.num {
  font-variant-numeric: tabular-nums;
}

.result-text {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  font-family: monospace;
}

.err-text {
  font-size: 12px;
  color: var(--el-color-danger);
}

.time-text {
  font-size: 12px;
  font-variant-numeric: tabular-nums;
  color: var(--el-text-color-secondary);
}

.pagination {
  display: flex;
  justify-content: flex-end;
  margin-top: 12px;
}
</style>
