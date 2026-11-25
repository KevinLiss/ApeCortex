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
              <template v-if="row.status === 'submitted' && row.can_review">
                <el-button link size="small" type="success" @click="handleReview(row, true)">
                  {{ t('knowledge.promotions.approve') }}
                </el-button>
                <el-button link size="small" type="danger" @click="handleReview(row, false)">
                  {{ t('knowledge.promotions.reject') }}
                </el-button>
              </template>
              <span v-else-if="row.status === 'submitted'" class="self-review-hint">
                {{ t('knowledge.promotions.selfReviewBlocked') }}
              </span>
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

    <!-- 公开库贡献榜 -->
    <section class="contrib-section">
      <div class="contrib-head">
        <h3>{{ t('knowledge.contributions.title') }}</h3>
        <p class="text-muted">{{ t('knowledge.contributions.subtitle') }}</p>
      </div>
      <div class="contrib-grid">
        <el-card shadow="never" class="contrib-board" v-loading="contribLoading">
          <div v-if="!contribBoard.length" class="contrib-empty">
            <el-empty :description="t('knowledge.contributions.empty')" :image-size="64" />
          </div>
          <ol v-else class="board-list">
            <li v-for="(item, idx) in contribBoard" :key="item.author_id" class="board-row">
              <span class="rank" :class="{ top: idx < 3 }">{{ idx + 1 }}</span>
              <span class="author">{{ item.author }}</span>
              <span class="count">{{ item.pages }} <em>{{ t('knowledge.contributions.pages') }}</em></span>
            </li>
          </ol>
          <p v-if="contribBoard.length" class="board-foot text-muted">{{ t('knowledge.contributions.foot') }}</p>
        </el-card>

        <el-card shadow="never" class="contrib-mine" v-loading="contribLoading">
          <div class="mine-label">{{ t('knowledge.contributions.mineLabel') }}</div>
          <div class="mine-nums">
            <div class="mine-num">
              <strong>{{ contribMine.approved ?? 0 }}</strong>
              <span>{{ t('knowledge.contributions.approved') }}</span>
            </div>
            <div class="mine-num">
              <strong>{{ contribMine.pending ?? 0 }}</strong>
              <span>{{ t('knowledge.contributions.pending') }}</span>
            </div>
          </div>
          <p class="mine-tip text-muted">{{ t('knowledge.contributions.mineTip') }}</p>
        </el-card>
      </div>
    </section>

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

import { getKbPromotions, getMyKbPromotions, reviewKbPromotion, getKbContributions } from '@/api'

const { t } = useI18n()

const tab = ref('review')
const loading = ref(false)
const contribLoading = ref(false)
const reviewItems = ref<any[]>([])
const myItems = ref<any[]>([])
const contribBoard = ref<any[]>([])
const contribMine = ref<any>({ approved: 0, pending: 0 })
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

async function fetchContrib() {
  contribLoading.value = true
  try {
    const res: any = await getKbContributions()
    contribBoard.value = res?.board ?? []
    contribMine.value = res?.mine ?? { approved: 0, pending: 0 }
  } finally {
    contribLoading.value = false
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

onMounted(() => {
  fetchAll()
  fetchContrib()
})
</script>

<style scoped>
.self-review-hint {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

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

/* 公开库贡献榜 */
.contrib-section {
  margin-top: 28px;
}

.contrib-head h3 {
  margin: 0 0 2px;
  font-size: 16px;
}

.contrib-head p {
  margin: 0 0 12px;
  font-size: 13px;
}

.contrib-grid {
  display: grid;
  grid-template-columns: minmax(0, 2fr) minmax(0, 1fr);
  gap: 16px;
}

@media (max-width: 900px) {
  .contrib-grid {
    grid-template-columns: 1fr;
  }
}

.board-list {
  list-style: none;
  margin: 0;
  padding: 0;
}

.board-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 9px 2px;
  border-bottom: 1px dashed var(--el-border-color-lighter);
}

.board-row:last-child {
  border-bottom: none;
}

.rank {
  flex: none;
  width: 22px;
  height: 22px;
  border-radius: 50%;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  background: var(--el-fill-color-light);
  color: var(--el-text-color-secondary);
  font-variant-numeric: tabular-nums;
}

.rank.top {
  background: var(--el-color-primary-light-8);
  color: var(--el-color-primary);
  font-weight: 600;
}

.author {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 14px;
}

.count {
  font-size: 13px;
  color: var(--el-text-color-regular);
  font-variant-numeric: tabular-nums;
}

.count em {
  font-style: normal;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.board-foot {
  margin: 10px 0 0;
  font-size: 12px;
}

.mine-label {
  margin-bottom: 14px;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

.mine-nums {
  display: flex;
  gap: 28px;
}

.mine-num {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.mine-num strong {
  font-size: 26px;
  line-height: 1.1;
  color: var(--el-color-primary);
  font-variant-numeric: tabular-nums;
}

.mine-num span {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.mine-tip {
  margin: 16px 0 0;
  font-size: 12px;
  line-height: 1.6;
}
</style>
