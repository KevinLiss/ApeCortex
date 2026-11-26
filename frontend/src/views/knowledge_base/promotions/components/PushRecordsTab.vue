<template>
  <div class="push-records-tab">
    <el-alert type="info" :closable="false" show-icon class="flow-alert"
      :title="t('knowledge.push.flowTitle')"
      :description="t('knowledge.push.flowDesc')" />

    <!-- 状态筛选 -->
    <div class="status-filter">
      <button
        v-for="st in statusChips"
        :key="st.key"
        class="status-chip"
        :class="{ active: status === st.key }"
        @click="switchStatus(st.key)"
      >{{ st.label }}</button>
    </div>

    <el-table :data="items" v-loading="loading" stripe>
      <el-table-column prop="doc_title" :label="t('knowledge.push.colTitle')" min-width="200" show-overflow-tooltip>
        <template #default="{ row }">
          <span class="doc-title" @click="openDetail(row)">{{ row.doc_title }}</span>
        </template>
      </el-table-column>
      <el-table-column :label="t('knowledge.push.colCategory')" width="120" align="center">
        <template #default="{ row }">
          <el-tag v-if="row.ai_category_name" size="small" effect="plain" round>{{ row.ai_category_name }}</el-tag>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
      <el-table-column :label="t('knowledge.push.colSubmitter')" width="90" align="center">
        <template #default="{ row }">#{{ row.submitted_by }}</template>
      </el-table-column>
      <el-table-column :label="t('knowledge.push.colStatus')" width="110" align="center">
        <template #default="{ row }">
          <el-tag size="small" :type="statusTag(row.status)" effect="light" round>
            {{ statusLabel(row.status) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="review_comment" :label="t('knowledge.push.colResult')" min-width="150" show-overflow-tooltip>
        <template #default="{ row }">
          <span class="muted">{{ row.review_comment || '—' }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="check_comment" :label="t('knowledge.push.colCheck')" min-width="150" show-overflow-tooltip>
        <template #default="{ row }">
          <span :class="row.check_comment ? 'check-done' : 'muted'">{{ row.check_comment || t('knowledge.push.notChecked') }}</span>
        </template>
      </el-table-column>
      <el-table-column :label="t('common.action.label')" width="150" fixed="right" align="center">
        <template #default="{ row }">
          <el-button link size="small" @click="openDetail(row)">{{ t('knowledge.push.view') }}</el-button>
          <el-button v-if="row.status === 'approved'" link size="small" type="primary" @click="openCheck(row)">
            {{ t('knowledge.push.check') }}
          </el-button>
        </template>
      </el-table-column>
      <template #empty>
        <el-empty :description="t('knowledge.push.empty')" :image-size="72" />
      </template>
    </el-table>

    <!-- 详情抽屉 -->
    <el-drawer v-model="detailVisible" :title="detail?.doc_title || ''" size="52%" :z-index="2000" class="kb-doc-drawer">
      <div v-if="detail" class="detail-body">
        <div class="detail-meta">
          <el-tag size="small" :type="statusTag(detail.status)" effect="light" round>{{ statusLabel(detail.status) }}</el-tag>
          <el-tag v-if="detail.ai_category_name" size="small" effect="plain" round>{{ detail.ai_category_name }}</el-tag>
          <span>#{{ detail.submitted_by }}</span>
          <span>{{ (detail.char_count || 0).toLocaleString() }} {{ t('knowledge.docs.charsUnit') }}</span>
        </div>
        <el-alert v-if="detail.ai_category_reason" type="info" :closable="false" show-icon class="reason-alert"
          :title="t('knowledge.push.aiReason')" :description="detail.ai_category_reason" />
        <div v-if="(detail.ai_split_preview || []).length" class="split-box">
          <div class="split-title">{{ t('knowledge.push.splitPreview') }}</div>
          <el-tag v-for="(st, i) in detail.ai_split_preview" :key="i" size="small" effect="plain" class="split-tag">
            {{ st }}
          </el-tag>
        </div>
        <div v-if="detail.review_comment" class="check-line">{{ t('knowledge.push.colResult') }}：{{ detail.review_comment }}</div>
        <div v-if="detail.check_comment" class="check-line">{{ t('knowledge.push.colCheck') }}：{{ detail.check_comment }}</div>
        <div class="detail-content" v-html="renderedContent" />
      </div>
    </el-drawer>

    <!-- 复查登记弹窗 -->
    <el-dialog v-model="checkVisible" :title="t('knowledge.push.checkTitle')" width="460px">
      <p class="check-doc">{{ checkRow?.doc_title }}</p>
      <el-input
        v-model="checkComment"
        type="textarea"
        :rows="3"
        maxlength="500"
        :placeholder="t('knowledge.push.checkPlaceholder')"
      />
      <template #footer>
        <el-button @click="checkVisible = false">{{ t('common.action.cancel') }}</el-button>
        <el-button type="primary" :loading="checking" @click="handleCheck">{{ t('knowledge.push.checkConfirm') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage } from 'element-plus'
import { marked } from 'marked'
import DOMPurify from 'dompurify'

import { checkKbSubmission, getKbSubmission, getKbSubmissions } from '@/api'

const { t } = useI18n()

const status = ref('all')
const items = ref<any[]>([])
const loading = ref(false)
const detailVisible = ref(false)
const detail = ref<any>(null)
const checkVisible = ref(false)
const checkRow = ref<any>(null)
const checkComment = ref('')
const checking = ref(false)

const statusChips = computed(() => [
  { key: 'all', label: t('knowledge.push.chipAll') },
  { key: 'approved', label: t('knowledge.push.chipApproved') },
  { key: 'ai_reviewing', label: t('knowledge.push.chipReviewing') },
])

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
    const res: any = await getKbSubmissions({ status: status.value, page: 1, page_size: 50 })
    items.value = res?.items ?? []
  } catch { /* 拦截器已提示 */ } finally {
    loading.value = false
  }
}

function switchStatus(key: string) {
  status.value = key
  fetchItems()
}

async function openDetail(row: any) {
  detailVisible.value = true
  try {
    detail.value = await getKbSubmission(row.id)
  } catch { /* 拦截器已提示 */ }
}

function openCheck(row: any) {
  checkRow.value = row
  checkComment.value = row.check_comment || ''
  checkVisible.value = true
}

async function handleCheck() {
  if (!checkRow.value) return
  checking.value = true
  try {
    await checkKbSubmission(checkRow.value.id, { comment: checkComment.value })
    ElMessage.success(t('knowledge.push.checked'))
    checkVisible.value = false
    fetchItems()
  } catch { /* 拦截器已提示 */ } finally {
    checking.value = false
  }
}

function statusTag(s: string): string {
  return {
    ai_reviewing: 'warning', approved: 'success',
    violation: 'danger', disposed: 'info',
  }[s] ?? 'info'
}

function statusLabel(s: string): string {
  const map: Record<string, string> = {
    ai_reviewing: t('knowledge.push.chipReviewing'),
    approved: t('knowledge.push.chipApproved'),
    violation: t('knowledge.violation.tabViolation'),
    disposed: t('knowledge.push.chipDisposed'),
  }
  return map[s] ?? s
}

onMounted(fetchItems)
defineExpose({ refresh: fetchItems })
</script>

<style scoped>
.flow-alert {
  margin-bottom: 12px;
}

.status-filter {
  display: flex;
  gap: 8px;
  margin-bottom: 12px;
}

.status-chip {
  padding: 4px 14px;
  border: 1px solid var(--el-border-color);
  border-radius: 14px;
  background: var(--el-bg-color);
  font-size: 13px;
  color: var(--el-text-color-regular);
  cursor: pointer;
  transition: all 0.2s;
}

.status-chip.active {
  border-color: var(--el-color-primary);
  color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
  font-weight: 600;
}

.doc-title {
  font-weight: 500;
  cursor: pointer;
}

.doc-title:hover {
  color: var(--el-color-primary);
}

.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.check-done {
  font-size: 12px;
  color: var(--el-color-success);
}

:deep(.el-drawer__body) {
  padding: 0 24px 24px;
}

.detail-body {
  padding: 0;
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

.split-box {
  margin-bottom: 16px;
  padding: 12px;
  border-radius: 10px;
  background: var(--el-fill-color-light);
}

.split-title {
  font-size: 13px;
  font-weight: 600;
  margin-bottom: 8px;
}

.split-tag {
  margin: 0 6px 6px 0;
}

.check-line {
  margin-bottom: 10px;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

.detail-content {
  font-size: 14px;
  line-height: 1.75;
  word-break: break-word;
}

.detail-content :deep(a) {
  color: var(--el-color-primary);
  word-break: break-all;
}

.detail-content :deep(ol),
.detail-content :deep(ul) {
  padding-left: 2em;
}

.check-doc {
  margin: 0 0 12px;
  font-weight: 600;
}
</style>
