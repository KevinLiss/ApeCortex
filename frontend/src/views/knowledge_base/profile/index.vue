<template>
  <div class="kb-profile-page">
    <div class="page-header">
      <h2>{{ t('knowledge.profile.title') }}</h2>
      <p class="text-muted">{{ t('knowledge.profile.subtitle') }}</p>
    </div>

    <!-- 统计卡片行（四等分等高，数值右对齐） -->
    <el-row :gutter="16" class="stat-row">
      <el-col :xs="12" :sm="6">
        <el-card shadow="never" class="stat-card">
          <div class="stat-value">{{ stats.total }}</div>
          <div class="stat-label">{{ t('knowledge.profile.statTotal') }}</div>
        </el-card>
      </el-col>
      <el-col :xs="12" :sm="6">
        <el-card shadow="never" class="stat-card">
          <div class="stat-value">{{ stats.byType.fact ?? 0 }}</div>
          <div class="stat-label">{{ t('knowledge.profile.statFact') }}</div>
        </el-card>
      </el-col>
      <el-col :xs="12" :sm="6">
        <el-card shadow="never" class="stat-card">
          <div class="stat-value">{{ stats.byType.preference ?? 0 }}</div>
          <div class="stat-label">{{ t('knowledge.profile.statPreference') }}</div>
        </el-card>
      </el-col>
      <el-col :xs="12" :sm="6">
        <el-card shadow="never" class="stat-card">
          <div class="stat-value">{{ (stats.byType.habit ?? 0) + (stats.byType.event ?? 0) }}</div>
          <div class="stat-label">{{ t('knowledge.profile.statHabit') }}</div>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="16">
      <!-- 左栏：画像 -->
      <el-col :xs="24" :md="10">
        <el-card shadow="never" class="profile-card">
          <template #header>
            <div class="card-header">
              <span>{{ t('knowledge.profile.profileCard') }}</span>
              <el-button
                type="primary"
                size="small"
                :loading="rewriting"
                @click="handleRewrite"
                v-permission="'knowledge_base:profile:rewrite'"
              >
                {{ rewriting ? t('knowledge.profile.rewriting') : t('knowledge.profile.rewrite') }}
              </el-button>
            </div>
          </template>
          <div class="profile-meta">
            <span class="text-muted">
              {{ t('knowledge.profile.lastRewrite') }}:
              {{ stats.lastRewriteAt || t('knowledge.profile.notRewritten') }}
            </span>
          </div>
          <el-alert
            :title="t('knowledge.profile.privacyAlert')"
            type="info"
            :closable="false"
            show-icon
            class="privacy-alert"
          />
          <div v-if="profileMd" class="profile-body" v-html="renderedProfile"></div>
          <el-empty v-else :description="t('knowledge.profile.profileEmpty')" :image-size="72" />
        </el-card>
      </el-col>

      <!-- 右栏：记忆条目 -->
      <el-col :xs="24" :md="14">
        <el-card shadow="never" class="memories-card">
          <template #header>
            <div class="card-header">
              <span>{{ t('knowledge.profile.memoriesTitle') }}</span>
              <el-input
                v-model="searchQuery"
                :placeholder="t('knowledge.profile.searchPlaceholder')"
                clearable
                style="width: 220px"
                @keyup.enter="fetchMemories"
                @clear="fetchMemories"
              />
            </div>
          </template>

          <el-tabs v-model="activeType" @tab-change="fetchMemories">
            <el-tab-pane name="all">
              <template #label>{{ t('knowledge.profile.typeAll') }} ({{ stats.total }})</template>
            </el-tab-pane>
            <el-tab-pane name="fact">
              <template #label>{{ t('knowledge.profile.typeFact') }} ({{ stats.byType.fact ?? 0 }})</template>
            </el-tab-pane>
            <el-tab-pane name="preference">
              <template #label>{{ t('knowledge.profile.typePreference') }} ({{ stats.byType.preference ?? 0 }})</template>
            </el-tab-pane>
            <el-tab-pane name="habit">
              <template #label>{{ t('knowledge.profile.typeHabit') }} ({{ stats.byType.habit ?? 0 }})</template>
            </el-tab-pane>
            <el-tab-pane name="event">
              <template #label>{{ t('knowledge.profile.typeEvent') }} ({{ stats.byType.event ?? 0 }})</template>
            </el-tab-pane>
          </el-tabs>

          <el-table :data="memories" v-loading="loading" stripe style="width: 100%">
            <el-table-column prop="content" :label="t('knowledge.profile.colContent')" min-width="240" show-overflow-tooltip />
            <el-table-column :label="t('knowledge.profile.colType')" width="100">
              <template #default="{ row }">
                <el-tag :type="typeTag(row.mem_type)" size="small" class="type-tag">
                  {{ t(`knowledge.profile.type${capitalize(row.mem_type)}`) }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column :label="t('knowledge.profile.colConfidence')" width="110" align="right">
              <template #default="{ row }">
                <el-progress
                  :percentage="Math.round(row.confidence * 100)"
                  :stroke-width="8"
                  class="confidence-bar"
                />
              </template>
            </el-table-column>
            <el-table-column prop="source_count" :label="t('knowledge.profile.colSourceCount')" width="70" align="right" />
            <el-table-column prop="updated_at" :label="t('knowledge.profile.colUpdatedAt')" width="170" />
            <el-table-column :label="t('knowledge.profile.colActions')" width="80" fixed="right">
              <template #default="{ row }">
                <el-button
                  link
                  type="danger"
                  size="small"
                  @click="handleDelete(row)"
                  v-permission="'knowledge_base:profile:delete'"
                >
                  {{ t('knowledge.profile.delete') }}
                </el-button>
              </template>
            </el-table-column>
            <template #empty>
              <el-empty
                v-if="searchQuery || activeType !== 'all'"
                :description="t('knowledge.profile.noSearchResult')"
                :image-size="72"
              />
              <el-empty v-else :description="t('knowledge.profile.empty')" :image-size="72" />
            </template>
          </el-table>

          <div class="pagination">
            <el-pagination
              v-model:current-page="page"
              v-model:page-size="pageSize"
              :total="total"
              :page-sizes="[10, 20, 50]"
              layout="total, sizes, prev, pager, next"
              @size-change="fetchMemories"
              @current-change="fetchMemories"
            />
          </div>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import { marked } from 'marked'
import DOMPurify from 'dompurify'

import {
  deleteKbMemory,
  getKbMemories,
  getKbProfile,
  getKbStats,
  rewriteKbProfile,
} from '@/api'

const { t } = useI18n()

// ---- state ----
const loading = ref(false)
const rewriting = ref(false)
const memories = ref<any[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const activeType = ref('all')
const searchQuery = ref('')
const profileMd = ref('')
const stats = ref<{ total: number; byType: Record<string, number>; lastRewriteAt: string | null }>({
  total: 0,
  byType: {},
  lastRewriteAt: null,
})

// ---- derived ----
// ---------- Markdown 渲染（与聊天页同惯例：gfm + DOMPurify 防 XSS）----------
marked.setOptions({
  breaks: true,
  gfm: true,
})

const renderedProfile = computed(() => {
  if (!profileMd.value) return ''
  try {
    const html = marked.parse(profileMd.value) as string
    return DOMPurify.sanitize(html)
  } catch {
    return profileMd.value
  }
})

// ---- helpers ----
function typeTag(type: string): string {
  switch (type) {
    case 'fact': return 'primary'
    case 'preference': return 'success'
    case 'habit': return 'warning'
    default: return 'info' // event
  }
}

function capitalize(s: string): string {
  return s ? s[0].toUpperCase() + s.slice(1) : s
}

// ---- fetch ----
async function fetchStats() {
  try {
    const res: any = await getKbStats()
    stats.value = res.data
  } catch {
    // 统计失败不打断页面
  }
}

async function fetchProfile() {
  try {
    const res: any = await getKbProfile()
    profileMd.value = res.data?.profile_md ?? ''
  } catch {
    // 画像失败不打断列表
  }
}

async function fetchMemories() {
  loading.value = true
  try {
    const params: any = {
      page: page.value,
      page_size: pageSize.value,
    }
    if (activeType.value !== 'all') params.mem_type = activeType.value
    if (searchQuery.value.trim()) params.q = searchQuery.value.trim()
    const res: any = await getKbMemories(params)
    memories.value = res.data?.items ?? []
    total.value = res.data?.total ?? 0
  } catch (e: any) {
    ElMessage.error(t('knowledge.profile.loadFailed'))
  } finally {
    loading.value = false
  }
}

// ---- actions ----
async function handleRewrite() {
  rewriting.value = true
  try {
    const res: any = await rewriteKbProfile()
    profileMd.value = res.data?.profile_md ?? ''
    ElMessage.success(res.msg || 'OK')
    fetchStats()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || e?.message || 'Error')
  } finally {
    rewriting.value = false
  }
}

async function handleDelete(row: any) {
  try {
    await ElMessageBox.confirm(
      t('knowledge.profile.deleteConfirmText'),
      t('knowledge.profile.deleteConfirmTitle'),
      { type: 'warning' }
    )
  } catch {
    return
  }
  try {
    await deleteKbMemory(row.id)
    ElMessage.success('OK')
    fetchMemories()
    fetchStats()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || e?.message || 'Error')
  }
}

onMounted(() => {
  fetchStats()
  fetchProfile()
  fetchMemories()
})
</script>

<style scoped>
.kb-profile-page {
  padding: 0 4px;
}

.page-header {
  margin-bottom: 16px;
}

.page-header h2 {
  margin: 0 0 4px;
  font-size: 20px;
}

.page-header p {
  margin: 0;
  font-size: 13px;
}

/* 统计卡片：等高、数值右对齐 */
.stat-row {
  margin-bottom: 16px;
}

.stat-card {
  height: 84px;
  box-sizing: border-box;
}

.stat-value {
  font-size: 26px;
  font-weight: 600;
  line-height: 1.2;
  text-align: right;
}

.stat-label {
  margin-top: 8px;
  font-size: 13px;
  color: var(--el-text-color-secondary);
  text-align: right;
}

/* 画像卡片 */
.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.profile-meta {
  margin-bottom: 8px;
  font-size: 12px;
}

.privacy-alert {
  margin-bottom: 12px;
}

.profile-body {
  font-size: 14px;
  line-height: 1.7;
  word-break: break-word;
}

.profile-body :deep(h2) {
  margin: 12px 0 6px;
  font-size: 16px;
}

.profile-body :deep(ul),
.profile-body :deep(p) {
  margin: 4px 0;
}

.memories-card :deep(.el-tabs__header) {
  margin-bottom: 8px;
}

.type-tag {
  width: 72px;
  text-align: center;
}

/* 置信度定宽进度条，数值列不抖动 */
.confidence-bar {
  width: 84px;
}

.confidence-bar :deep(.el-progress__text) {
  font-size: 11px;
  min-width: 34px;
}

.pagination {
  display: flex;
  justify-content: flex-end;
  margin-top: 12px;
}

/* 移动端堆叠时上下留白 */
@media (max-width: 992px) {
  .el-row :deep(.el-col) {
    margin-bottom: 16px;
  }
}
</style>
