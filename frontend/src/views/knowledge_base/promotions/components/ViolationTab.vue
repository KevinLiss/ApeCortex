<template>
  <div class="violation-tab">
    <el-alert type="warning" :closable="false" show-icon class="tip-alert"
      :title="t('knowledge.violation.tipTitle')" :description="t('knowledge.violation.tipDesc')" />

    <el-table :data="items" v-loading="loading" stripe>
      <el-table-column prop="doc_title" :label="t('knowledge.push.colTitle')" min-width="180" show-overflow-tooltip>
        <template #default="{ row }">
          <span class="doc-title" @click="openDetail(row)">{{ row.doc_title }}</span>
        </template>
      </el-table-column>
      <el-table-column :label="t('knowledge.violation.colRisk')" width="150" align="center">
        <template #default="{ row }">
          <el-tag size="small" :type="levelTag(row.ai_risk_level)" effect="light" round>
            {{ riskLabel(row.ai_risk_type) }}
          </el-tag>
          <span v-if="row.ai_risk_level !== 'none'" class="risk-level">{{ levelLabel(row.ai_risk_level) }}</span>
        </template>
      </el-table-column>
      <el-table-column :label="t('knowledge.violation.colReason')" min-width="220">
        <template #default="{ row }">
          <span class="reason-text">{{ row.ai_category_reason || '—' }}</span>
        </template>
      </el-table-column>
      <el-table-column :label="t('knowledge.push.colSubmitter')" width="90" align="center">
        <template #default="{ row }">#{{ row.submitted_by }}</template>
      </el-table-column>
      <el-table-column :label="t('common.action.label')" width="230" fixed="right" align="center">
        <template #default="{ row }">
          <el-button link size="small" @click="openDetail(row)">{{ t('knowledge.push.view') }}</el-button>
          <el-button link size="small" type="primary" @click="handleDispose(row, 'kept_evidence')">
            {{ t('knowledge.violation.keepEvidence') }}
          </el-button>
          <el-button link size="small" type="danger" @click="handleDispose(row, 'deleted')">
            {{ t('knowledge.violation.deleteContent') }}
          </el-button>
        </template>
      </el-table-column>
      <template #empty>
        <el-empty :description="t('knowledge.violation.empty')" :image-size="72" />
      </template>
    </el-table>

    <!-- 详情抽屉 -->
    <el-drawer v-model="detailVisible" :title="detail?.doc_title || ''" size="52%" :z-index="2000" class="kb-doc-drawer">
      <div v-if="detail" class="detail-body">
        <div class="detail-meta">
          <el-tag size="small" :type="levelTag(detail.ai_risk_level)" effect="light" round>
            {{ riskLabel(detail.ai_risk_type) }} · {{ levelLabel(detail.ai_risk_level) }}
          </el-tag>
          <span>#{{ detail.submitted_by }}</span>
          <span>{{ (detail.char_count || 0).toLocaleString() }} {{ t('knowledge.docs.charsUnit') }}</span>
        </div>
        <el-alert v-if="detail.ai_category_reason" type="warning" :closable="false" show-icon class="reason-alert"
          :title="t('knowledge.violation.aiJudge')" :description="detail.ai_category_reason" />
        <div class="detail-content" v-html="renderedContent" />
      </div>
    </el-drawer>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import { marked } from 'marked'
import DOMPurify from 'dompurify'

import { disposeKbSubmission, getKbSubmission, getKbSubmissions } from '@/api'

const { t } = useI18n()

const items = ref<any[]>([])
const loading = ref(false)
const detailVisible = ref(false)
const detail = ref<any>(null)

const renderedContent = computed(() => {
  if (!detail.value?.doc_content) return ''
  try {
    return DOMPurify.sanitize(marked.parse(detail.value.doc_content) as string)
  } catch {
    return detail.value.doc_content
  }
})

async function fetchItems() {
  loading.value = true
  try {
    const res: any = await getKbSubmissions({ status: 'violation', page: 1, page_size: 50 })
    items.value = res?.items ?? []
  } catch { /* 拦截器已提示 */ } finally {
    loading.value = false
  }
}

async function openDetail(row: any) {
  detailVisible.value = true
  try {
    detail.value = await getKbSubmission(row.id)
  } catch { /* 拦截器已提示 */ }
}

async function handleDispose(row: any, action: 'deleted' | 'kept_evidence') {
  const key = action === 'deleted' ? 'knowledge.violation.deleteConfirm' : 'knowledge.violation.keepConfirm'
  try {
    await ElMessageBox.confirm(t(key), { type: 'warning' })
  } catch { return }
  try {
    await disposeKbSubmission(row.id, { action })
    ElMessage.success(t('knowledge.violation.disposed'))
    fetchItems()
  } catch { /* 拦截器已提示 */ }
}

function levelTag(level: string): string {
  return { high: 'danger', medium: 'warning', low: 'info', none: 'info' }[level] ?? 'info'
}

function riskLabel(rt: string): string {
  const map: Record<string, string> = {
    politics: t('knowledge.violation.riskPolitics'),
    porn: t('knowledge.violation.riskPorn'),
    violence: t('knowledge.violation.riskViolence'),
    copyright: t('knowledge.violation.riskCopyright'),
    ad: t('knowledge.violation.riskAd'),
    privacy: t('knowledge.violation.riskPrivacy'),
    none: t('knowledge.violation.riskNone'),
  }
  return map[rt] ?? rt
}

function levelLabel(level: string): string {
  const map: Record<string, string> = {
    high: t('knowledge.violation.levelHigh'),
    medium: t('knowledge.violation.levelMedium'),
    low: t('knowledge.violation.levelLow'),
    none: t('knowledge.violation.riskNone'),
  }
  return map[level] ?? level
}

onMounted(fetchItems)
defineExpose({ refresh: fetchItems })
</script>

<style scoped>
.tip-alert {
  margin-bottom: 12px;
}

.doc-title {
  font-weight: 500;
  cursor: pointer;
}

.doc-title:hover {
  color: var(--el-color-primary);
}

.risk-level {
  margin-left: 4px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.reason-text {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.detail-body {
  padding: 0;
}

:deep(.el-drawer__body) {
  padding: 0 24px 24px;
}

.detail-meta {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.reason-alert {
  margin-bottom: 14px;
}

.detail-content {
  font-size: 14px;
  line-height: 1.75;
  word-break: break-word;
}

/* 引用列表：marker 挂在内容框内（全局 reset 清了 ul padding 会导致序号贴边溢出） */
.detail-content :deep(ol),
.detail-content :deep(ul),
.page-body :deep(ol),
.page-body :deep(ul),
.doc-content :deep(ol),
.doc-content :deep(ul) {
  padding-left: 2em;
  margin: 10px 0;
  list-style-position: outside;
}

.detail-content :deep(li::marker),
.page-body :deep(li::marker) {
  color: var(--el-text-color-secondary);
}
</style>
