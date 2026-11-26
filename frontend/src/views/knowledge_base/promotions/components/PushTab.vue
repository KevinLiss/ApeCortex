<template>
  <div class="push-tab">
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
          <div class="title-cell">
            <span class="doc-title" @click="openDetail(row)">{{ row.doc_title }}</span>
            <el-tag v-if="row.ai_category_name" size="small" effect="plain" round>
              {{ row.ai_category_name }}
            </el-tag>
          </div>
        </template>
      </el-table-column>
      <el-table-column :label="t('knowledge.push.colSubmitter')" width="90" align="center">
        <template #default="{ row }">#{{ row.submitted_by }}</template>
      </el-table-column>
      <el-table-column :label="t('knowledge.push.colChars')" width="90" align="center">
        <template #default="{ row }">{{ (row.char_count || 0).toLocaleString() }}</template>
      </el-table-column>
      <el-table-column :label="t('knowledge.push.colSplit')" width="140" align="center">
        <template #default="{ row }">
          <el-tooltip v-if="(row.ai_split_preview || []).length" :disabled="(row.ai_split_preview || []).length <= 3"
            :content="(row.ai_split_preview || []).join(' / ')" placement="top">
            <el-tag size="small" type="success" effect="light" round>
              {{ row.ai_split_preview.length }} {{ t('knowledge.push.splitUnit') }}
            </el-tag>
          </el-tooltip>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
      <el-table-column :label="t('common.action.label')" width="220" fixed="right" align="center">
        <template #default="{ row }">
          <el-button link size="small" @click="openDetail(row)">{{ t('knowledge.push.view') }}</el-button>
          <template v-if="status === 'pending'">
            <el-button link size="small" type="success" @click="openReview(row)">
              {{ t('knowledge.promotions.approve') }}
            </el-button>
            <el-button link size="small" type="danger" @click="handleReject(row)">
              {{ t('knowledge.promotions.reject') }}
            </el-button>
          </template>
        </template>
      </el-table-column>
      <template #empty>
        <el-empty :description="t('knowledge.push.empty')" :image-size="72" />
      </template>
    </el-table>

    <!-- 详情抽屉 -->
    <el-drawer v-model="detailVisible" :title="detail?.doc_title || ''" size="52%" :z-index="2000">
      <div v-if="detail" class="detail-body">
        <div class="detail-meta">
          <el-tag size="small" effect="plain" round>#{{ detail.submitted_by }}</el-tag>
          <span v-if="detail.ai_category_name">
            {{ t('knowledge.push.aiCategory') }}: {{ detail.ai_category_name }}
          </span>
          <span>{{ (detail.char_count || 0).toLocaleString() }} {{ t('knowledge.docs.charsUnit') }}</span>
        </div>
        <el-alert v-if="detail.ai_category_reason" type="info" :closable="false" show-icon class="reason-alert"
          :title="t('knowledge.push.aiReason')" :description="detail.ai_category_reason" />
        <div v-if="(detail.ai_split_preview || []).length" class="split-box">
          <div class="split-title">{{ t('knowledge.push.splitPreview') }}</div>
          <el-tag v-for="(s, i) in detail.ai_split_preview" :key="i" size="small" effect="plain" class="split-tag">
            {{ s }}
          </el-tag>
        </div>
        <div class="detail-content" v-html="renderedContent" />
      </div>
    </el-drawer>

    <!-- 审核弹窗 -->
    <el-dialog v-model="reviewVisible" :title="t('knowledge.push.reviewTitle')" width="480px">
      <el-form label-position="top" v-if="reviewRow">
        <el-form-item :label="t('knowledge.push.reviewDoc')">
          <span class="review-doc">{{ reviewRow.doc_title }}</span>
        </el-form-item>
        <el-form-item :label="t('knowledge.push.reviewCategory')">
          <el-select v-model="reviewCategory" style="width: 100%" clearable>
            <el-option v-for="c in flatCategories" :key="c.id" :value="c.id"
              :label="(c.parent_id ? '└ ' : '') + c.name" />
          </el-select>
          <div v-if="reviewRow.ai_category_name && !reviewCategory" class="ai-suggest">
            {{ t('knowledge.push.aiSuggest') }}: {{ reviewRow.ai_category_name }}
          </div>
        </el-form-item>
        <el-form-item :label="t('knowledge.push.reviewTarget')">
          <el-select v-model="reviewTarget" style="width: 100%">
            <el-option v-for="s in targets" :key="s.id" :value="s.id" :label="s.name" />
          </el-select>
        </el-form-item>
        <el-form-item :label="t('knowledge.push.reviewComment')">
          <el-input v-model="reviewComment" type="textarea" :rows="2" maxlength="200" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="reviewVisible = false">{{ t('common.action.cancel') }}</el-button>
        <el-button type="primary" @click="handleApprove">{{ t('knowledge.promotions.approve') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import { marked } from 'marked'
import DOMPurify from 'dompurify'

import {
  getKbCategories,
  getKbPushTargets,
  getKbSubmission,
  getKbSubmissions,
  reviewKbSubmission,
} from '@/api'

const { t } = useI18n()

const status = ref('pending')
const items = ref<any[]>([])
const loading = ref(false)
const detailVisible = ref(false)
const detail = ref<any>(null)
const reviewVisible = ref(false)
const reviewRow = ref<any>(null)
const reviewCategory = ref<number | null>(null)
const reviewTarget = ref<number | null>(null)
const reviewComment = ref('')
const categories = ref<any[]>([])
const targets = ref<any[]>([])

const statusChips = computed(() => [
  { key: 'pending', label: t('knowledge.push.chipPending') },
  { key: 'approved', label: t('knowledge.push.chipApproved') },
  { key: 'rejected', label: t('knowledge.push.chipRejected') },
])

const flatCategories = computed(() =>
  categories.value.flatMap((c) => [c, ...(c.children || [])].map(({ children, ...rest }: any) => rest)),
)

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

async function fetchMeta() {
  try {
    const [cats, tgts]: any[] = [await getKbCategories(), await getKbPushTargets()]
    categories.value = cats?.items ?? []
    targets.value = tgts?.items ?? []
  } catch { /* 拦截器已提示 */ }
}

async function openDetail(row: any) {
  detailVisible.value = true
  try {
    detail.value = await getKbSubmission(row.id)
  } catch { /* 拦截器已提示 */ }
}

function openReview(row: any) {
  reviewRow.value = row
  reviewCategory.value = row.ai_category_id ?? null
  reviewTarget.value = row.target_space_id ?? targets.value[0]?.id ?? null
  reviewComment.value = ''
  reviewVisible.value = true
}

async function handleApprove() {
  if (!reviewRow.value || !reviewTarget.value) return
  try {
    await reviewKbSubmission(reviewRow.value.id, {
      approve: true,
      category_id: reviewCategory.value ?? undefined,
      target_space_id: reviewTarget.value,
      comment: reviewComment.value,
    })
    ElMessage.success(t('knowledge.push.approved'))
    reviewVisible.value = false
    fetchItems()
  } catch { /* 拦截器已提示 */ }
}

async function handleReject(row: any) {
  let comment = ''
  try {
    const { value } = await ElMessageBox.prompt(
      t('knowledge.push.rejectPrompt'), t('knowledge.promotions.reject'), { inputPlaceholder: '' }
    )
    comment = value || ''
  } catch { return }
  try {
    await reviewKbSubmission(row.id, { approve: false, comment })
    ElMessage.success(t('knowledge.push.rejected'))
    fetchItems()
  } catch { /* 拦截器已提示 */ }
}

onMounted(() => {
  fetchMeta()
  fetchItems()
})
defineExpose({ refresh: fetchItems })
</script>

<style scoped>
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

.title-cell {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.doc-title {
  font-weight: 500;
  cursor: pointer;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.doc-title:hover {
  color: var(--el-color-primary);
}

.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.detail-body {
  padding: 0 4px;
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

.detail-content {
  font-size: 14px;
  line-height: 1.75;
  word-break: break-word;
}

.review-doc {
  font-weight: 600;
}

.ai-suggest {
  margin-top: 4px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
