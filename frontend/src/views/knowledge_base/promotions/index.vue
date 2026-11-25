<template>
  <div class="kb-promotions-page">
    <div class="page-header">
      <div>
        <h2>{{ t('knowledge.promotions.title') }}</h2>
        <p class="text-muted">{{ t('knowledge.promotions.subtitle') }}</p>
      </div>
    </div>

    <el-tabs v-model="tab">
      <el-tab-pane :label="t('knowledge.promotions.tabReview')" name="review">
        <el-table :data="reviewItems" v-loading="loading" stripe>
          <el-table-column prop="snapshot_title" :label="t('knowledge.promotions.colTitle')" min-width="180" show-overflow-tooltip />
          <el-table-column prop="submit_note" :label="t('knowledge.promotions.colNote')" min-width="180" show-overflow-tooltip />
          <el-table-column :label="t('knowledge.promotions.colSubmitter')" width="100" align="center">
            <template #default="{ row }">#{{ row.submitted_by }}</template>
          </el-table-column>
          <el-table-column :label="t('knowledge.promotions.colStatus')" width="90" align="center">
            <template #default="{ row }">
              <el-tag size="small" :type="promoTag(row.status)" effect="light" round>{{ promoLabel(row.status) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column :label="t('knowledge.promotions.colActions')" width="200" fixed="right" align="center">
            <template #default="{ row }">
              <el-button link size="small" @click="openSnapshot(row)">{{ t('knowledge.promotions.viewSnapshot') }}</el-button>
              <el-button v-if="row.status === 'submitted'" link size="small" type="success" @click="handleReview(row, true)">
                {{ t('knowledge.promotions.approve') }}
              </el-button>
              <el-button v-if="row.status === 'submitted'" link size="small" type="danger" @click="handleReview(row, false)">
                {{ t('knowledge.promotions.reject') }}
              </el-button>
            </template>
          </el-table-column>
          <template #empty>
            <el-empty :description="t('knowledge.promotions.empty')" :image-size="72" />
          </template>
        </el-table>
      </el-tab-pane>

      <el-tab-pane :label="t('knowledge.promotions.tabMine')" name="mine">
        <el-table :data="myItems" v-loading="loading" stripe>
          <el-table-column prop="snapshot_title" :label="t('knowledge.promotions.colTitle')" min-width="200" show-overflow-tooltip />
          <el-table-column prop="submit_note" :label="t('knowledge.promotions.colNote')" min-width="180" show-overflow-tooltip />
          <el-table-column :label="t('knowledge.promotions.colStatus')" width="110" align="center">
            <template #default="{ row }">
              <el-tag size="small" :type="promoTag(row.status)" effect="light" round>{{ promoLabel(row.status) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="review_comment" :label="t('knowledge.promotions.reviewComment')" min-width="160" show-overflow-tooltip />
          <el-table-column :label="t('knowledge.promotions.colActions')" width="120" fixed="right" align="center">
            <template #default="{ row }">
              <el-button link size="small" @click="openSnapshot(row)">{{ t('knowledge.promotions.viewSnapshot') }}</el-button>
            </template>
          </el-table-column>
          <template #empty>
            <el-empty :description="t('knowledge.promotions.empty')" :image-size="72" />
          </template>
        </el-table>
      </el-tab-pane>
    </el-tabs>

    <!-- 快照预览 -->
    <el-dialog v-model="snapshotVisible" :title="t('knowledge.promotions.snapshot')" width="640px" top="6vh">
      <div class="snapshot-body" v-html="snapshotHtml"></div>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import { marked } from 'marked'
import DOMPurify from 'dompurify'

import { getKbPromotions, getMyKbPromotions, reviewKbPromotion } from '@/api'

const { t } = useI18n()

const tab = ref('review')
const loading = ref(false)
const reviewItems = ref<any[]>([])
const myItems = ref<any[]>([])
const snapshotVisible = ref(false)
const snapshotHtml = ref('')

function promoTag(s: string): string {
  return { submitted: 'warning', approved: 'success', rejected: 'danger', withdrawn: 'info', draft: 'info' }[s] ?? 'info'
}
function promoLabel(s: string): string {
  const map: Record<string, string> = {
    submitted: t('knowledge.promotions.statusSubmitted'),
    approved: t('knowledge.promotions.statusApproved'),
    rejected: t('knowledge.promotions.statusRejected'),
    withdrawn: t('knowledge.promotions.statusWithdrawn'),
    draft: t('knowledge.promotions.statusDraft'),
  }
  return map[s] ?? s
}

async function fetchAll() {
  loading.value = true
  try {
    const [rev, mine]: any[] = [await getKbPromotions(), await getMyKbPromotions()]
    reviewItems.value = rev?.items ?? []
    myItems.value = mine?.items ?? []
  } finally {
    loading.value = false
  }
}

function openSnapshot(row: any) {
  const md = row.snapshot_md || ''
  try {
    snapshotHtml.value = DOMPurify.sanitize(marked.parse(md) as string)
  } catch {
    snapshotHtml.value = md
  }
  snapshotVisible.value = true
}

async function handleReview(row: any, approve: boolean) {
  try {
    if (approve) {
      await ElMessageBox.confirm(t('knowledge.promotions.approveConfirm'), '', { type: 'warning' })
    }
  } catch { return }

  let comment = ''
  if (!approve) {
    try {
      const { value } = await ElMessageBox.prompt(t('knowledge.promotions.reviewComment'), '', { inputType: 'textarea' })
      comment = value || ''
    } catch { return }
  }
  try {
    await reviewKbPromotion(row.id, approve, comment)
    ElMessage.success(t('knowledge.promotions.reviewDone'))
    fetchAll()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.msg || e?.message || 'Error')
  }
}

onMounted(fetchAll)
</script>

<style scoped>
.kb-promotions-page .page-header {
  margin-bottom: 16px;
}

.page-header h2 {
  margin: 0 0 4px;
  font-size: 20px;
}

.snapshot-body {
  font-size: 14px;
  line-height: 1.75;
  max-height: 60vh;
  overflow: auto;
  word-break: break-word;
}
</style>
