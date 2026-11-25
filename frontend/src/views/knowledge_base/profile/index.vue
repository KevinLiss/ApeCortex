<template>
  <div class="kb-profile-page">
    <!-- ===== 顶部身份条：档案式头部（参考 Dify 知识库详情页头）===== -->
    <div class="hero">
      <div class="hero-avatar">
        <span class="hero-avatar-text">{{ avatarText }}</span>
        <el-tooltip :content="t('knowledge.profile.privacyTip')" placement="bottom">
          <el-icon class="hero-lock"><Lock /></el-icon>
        </el-tooltip>
      </div>
      <div class="hero-main">
        <div class="hero-name-line">
          <span class="hero-name">{{ userNickname }}</span>
          <el-tag size="small" effect="dark" round class="hero-role">{{ userRole }}</el-tag>
        </div>
        <div class="hero-sub">{{ t('knowledge.profile.subtitle') }}</div>
        <!-- 元数据行：等距分隔，固定槽位 -->
        <div class="hero-meta">
          <span class="meta-item">
            <span class="meta-value">{{ stats.total }}</span>
            <span class="meta-label">{{ t('knowledge.profile.statTotal') }}</span>
          </span>
          <span class="meta-divider"></span>
          <span class="meta-item">
            <span class="meta-value">{{ stats.byType?.fact ?? 0 }}</span>
            <span class="meta-label">{{ t('knowledge.profile.statFact') }}</span>
          </span>
          <span class="meta-divider"></span>
          <span class="meta-item">
            <span class="meta-value">{{ stats.byType?.preference ?? 0 }}</span>
            <span class="meta-label">{{ t('knowledge.profile.statPreference') }}</span>
          </span>
          <span class="meta-divider"></span>
          <span class="meta-item">
            <span class="meta-value">{{ (stats.byType?.habit ?? 0) + (stats.byType?.event ?? 0) }}</span>
            <span class="meta-label">{{ t('knowledge.profile.statHabit') }}</span>
          </span>
        </div>
      </div>
      <div class="hero-actions">
        <el-button
          type="primary"
          :loading="rewriting"
          @click="handleRewrite"
          v-permission="'knowledge_base:profile:rewrite'"
        >
          <el-icon><MagicStick /></el-icon>
          <span>{{ rewriting ? t('knowledge.profile.rewriting') : t('knowledge.profile.rewrite') }}</span>
        </el-button>
        <div class="hero-last">{{ t('knowledge.profile.lastRewrite') }}: {{ lastRewriteText }}</div>
      </div>
    </div>

    <!-- ===== 主体：档案分区 ===== -->
    <el-row :gutter="16" class="body">
      <!-- 左：画像卡片 -->
      <el-col :xs="24" :lg="9">
        <el-card shadow="never" class="panel">
          <template #header>
            <div class="panel-header">
              <el-icon class="panel-icon"><User /></el-icon>
              <span>{{ t('knowledge.profile.profileCard') }}</span>
              <el-tag v-if="profileVersion > 0" size="small" type="info" effect="plain" class="ver-tag">
                v{{ profileVersion }}
              </el-tag>
            </div>
          </template>
          <div v-if="profileMd" class="profile-body" v-html="renderedProfile"></div>
          <div v-else class="profile-empty">
            <el-empty :description="t('knowledge.profile.profileEmpty')" :image-size="80" />
          </div>
        </el-card>
      </el-col>

      <!-- 右：记忆库 -->
      <el-col :xs="24" :lg="15">
        <el-card shadow="never" class="panel">
          <template #header>
            <div class="panel-header">
              <el-icon class="panel-icon"><Collection /></el-icon>
              <span>{{ t('knowledge.profile.memoriesTitle') }}</span>
              <span class="count-badge">{{ stats.total }}</span>
              <div class="header-spacer"></div>
              <el-input
                v-model="searchQuery"
                :prefix-icon="Search"
                :placeholder="t('knowledge.profile.searchPlaceholder')"
                clearable
                class="search-input"
                @keyup.enter="fetchMemories"
                @clear="fetchMemories"
              />
            </div>
          </template>

          <!-- 类型筛选：分段器样式 -->
          <div class="type-filter">
            <button
              v-for="tp in typeTabs"
              :key="tp.key"
              class="type-chip"
              :class="{ active: activeType === tp.key }"
              @click="switchType(tp.key)"
            >
              <span class="chip-dot" :data-type="tp.key"></span>
              {{ tp.label }}
              <span class="chip-count">{{ tp.count }}</span>
            </button>
          </div>

          <!-- 记忆时间线：档案条目式 -->
          <div v-loading="loading" class="memory-list">
            <div v-if="memories.length" class="timeline">
              <div v-for="m in memories" :key="m.id" class="memory-row">
                <div class="row-rail">
                  <span class="row-dot" :data-type="m.mem_type"></span>
                </div>
                <div class="row-main">
                  <div class="row-content">{{ m.content }}</div>
                  <div class="row-meta">
                    <el-tag size="small" :type="typeTag(m.mem_type)" effect="light" round class="type-tag">
                      {{ typeLabel(m.mem_type) }}
                    </el-tag>
                    <div class="confidence-wrap">
                      <div class="confidence-track">
                        <div class="confidence-fill" :style="{ width: `${Math.round(m.confidence * 100)}%` }"></div>
                      </div>
                      <span class="confidence-num">{{ Math.round(m.confidence * 100) }}%</span>
                    </div>
                    <span class="row-evidence" :title="t('knowledge.profile.colSourceCount')">
                      <el-icon><Link /></el-icon>{{ m.source_count }}
                    </span>
                    <span class="row-time">{{ formatTime(m.updated_at) }}</span>
                    <span class="header-spacer"></span>
                    <el-button
                      link
                      type="danger"
                      size="small"
                      class="row-delete"
                      @click="handleDelete(m)"
                      v-permission="'knowledge_base:profile:delete'"
                    >
                      {{ t('knowledge.profile.delete') }}
                    </el-button>
                  </div>
                </div>
              </div>
            </div>
            <el-empty
              v-else-if="searchQuery || activeType !== 'all'"
              :description="t('knowledge.profile.noSearchResult')"
              :image-size="80"
            />
            <div v-else class="first-empty">
              <div class="first-empty-icon">
                <el-icon :size="26"><ChatDotRound /></el-icon>
              </div>
              <div class="first-empty-title">{{ t('knowledge.profile.emptyTitle') }}</div>
              <div class="first-empty-desc">{{ t('knowledge.profile.emptyDesc') }}</div>
            </div>
          </div>

          <div class="pagination" v-if="total > pageSize">
            <el-pagination
              v-model:current-page="page"
              v-model:page-size="pageSize"
              :total="total"
              :page-sizes="[10, 20, 50]"
              layout="total, prev, pager, next"
              small
              background
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
import { ChatDotRound, Collection, Link, Lock, MagicStick, Search, User } from '@element-plus/icons-vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'

import { useUserStore } from '@/stores/user'
import {
  deleteKbMemory,
  getKbMemories,
  getKbProfile,
  getKbStats,
  rewriteKbProfile,
} from '@/api'

const { t } = useI18n()
const userStore = useUserStore()

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
const profileVersion = ref(0)
const stats = ref<{ total: number; byType: Record<string, number>; lastRewriteAt: string | null }>({
  total: 0,
  byType: {},
  lastRewriteAt: null,
})

// ---- derived ----
const userNickname = computed(() => userStore.nickname || userStore.username || 'User')
const userRole = computed(() => userStore.roles?.[0] || 'Member')
const avatarText = computed(() => (userNickname.value || 'U').trim().charAt(0).toUpperCase())

const typeTabs = computed(() => [
  { key: 'all', label: t('knowledge.profile.typeAll'), count: stats.value.total },
  { key: 'fact', label: t('knowledge.profile.typeFact'), count: stats.value.byType?.fact ?? 0 },
  { key: 'preference', label: t('knowledge.profile.typePreference'), count: stats.value.byType?.preference ?? 0 },
  { key: 'habit', label: t('knowledge.profile.typeHabit'), count: stats.value.byType?.habit ?? 0 },
  { key: 'event', label: t('knowledge.profile.typeEvent'), count: stats.value.byType?.event ?? 0 },
])

const lastRewriteText = computed(() => stats.value.lastRewriteAt
  ? formatTime(stats.value.lastRewriteAt)
  : t('knowledge.profile.notRewritten'))

marked.setOptions({ breaks: true, gfm: true })

const renderedProfile = computed(() => {
  if (!profileMd.value) return ''
  try {
    return DOMPurify.sanitize(marked.parse(profileMd.value) as string)
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
    default: return 'info'
  }
}

function typeLabel(type: string): string {
  const map: Record<string, string> = {
    fact: t('knowledge.profile.typeFact'),
    preference: t('knowledge.profile.typePreference'),
    habit: t('knowledge.profile.typeHabit'),
    event: t('knowledge.profile.typeEvent'),
  }
  return map[type] ?? type
}

function formatTime(value: string | null): string {
  if (!value) return '-'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return value
  const now = new Date()
  const diffMin = Math.floor((now.getTime() - d.getTime()) / 60000)
  if (diffMin < 1) return t('knowledge.profile.justNow')
  if (diffMin < 60) return `${diffMin} ${t('knowledge.profile.minAgo')}`
  const diffHour = Math.floor(diffMin / 60)
  if (diffHour < 24) return `${diffHour} ${t('knowledge.profile.hourAgo')}`
  const ymd = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
  return ymd
}

// ---- fetch ----
async function fetchStats() {
  try {
    const res: any = await getKbStats()
    stats.value = {
      total: res?.total ?? 0,
      byType: res?.by_type ?? res?.byType ?? {},
      lastRewriteAt: res?.last_rewrite_at ?? res?.lastRewriteAt ?? null,
    }
  } catch { /* 统计失败不打断页面 */ }
}

async function fetchProfile() {
  try {
    const res: any = await getKbProfile()
    profileMd.value = res?.profile_md ?? ''
    profileVersion.value = res?.version ?? 0
  } catch { /* 画像失败不打断列表 */ }
}

async function fetchMemories() {
  loading.value = true
  try {
    const params: any = { page: page.value, page_size: pageSize.value }
    if (activeType.value !== 'all') params.mem_type = activeType.value
    if (searchQuery.value.trim()) params.q = searchQuery.value.trim()
    const res: any = await getKbMemories(params)
    memories.value = res?.items ?? []
    total.value = res?.total ?? 0
  } catch {
    ElMessage.error(t('knowledge.profile.loadFailed'))
  } finally {
    loading.value = false
  }
}

function switchType(key: string) {
  activeType.value = key
  page.value = 1
  fetchMemories()
}

// ---- actions ----
async function handleRewrite() {
  rewriting.value = true
  try {
    const res: any = await rewriteKbProfile()
    profileMd.value = res?.profile_md ?? ''
    profileVersion.value = res?.version ?? (profileVersion.value + 1)
    ElMessage.success(t('knowledge.profile.rewriteDone'))
    fetchStats()
    fetchMemories()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.msg || e?.message || t('knowledge.profile.loadFailed'))
  } finally {
    rewriting.value = false
  }
}

async function handleDelete(row: any) {
  try {
    await ElMessageBox.confirm(
      t('knowledge.profile.deleteConfirmText'),
      t('knowledge.profile.deleteConfirmTitle'),
      { type: 'warning', confirmButtonText: t('knowledge.profile.delete'), cancelButtonText: t('common.action.cancel') }
    )
  } catch { return }
  try {
    await deleteKbMemory(row.id)
    ElMessage.success(t('knowledge.profile.deleteDone'))
    fetchMemories()
    fetchStats()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.msg || e?.message || 'Error')
  }
}

onMounted(() => {
  fetchStats()
  fetchProfile()
  fetchMemories()
})
</script>

<style scoped>
/* ============ 页面容器 ============ */
.kb-profile-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

/* ============ 顶部身份条（档案头部） ============ */
.hero {
  display: flex;
  align-items: center;
  gap: 20px;
  padding: 20px 24px;
  background: linear-gradient(135deg,
    var(--el-color-primary-light-9) 0%,
    var(--el-bg-color) 55%);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 10px;
}

.hero-avatar {
  position: relative;
  flex-shrink: 0;
}

.hero-avatar-text {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 56px;
  height: 56px;
  border-radius: 50%;
  font-size: 22px;
  font-weight: 600;
  color: #fff;
  background: linear-gradient(135deg, var(--el-color-primary), var(--el-color-primary-light-3));
}

.hero-lock {
  position: absolute;
  right: -2px;
  bottom: -2px;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  border-radius: 50%;
  color: #fff;
  font-size: 11px;
  background: var(--el-color-success);
  border: 2px solid var(--el-bg-color);
}

.hero-main {
  flex: 1;
  min-width: 0;
}

.hero-name-line {
  display: flex;
  align-items: center;
  gap: 10px;
}

.hero-name {
  font-size: 20px;
  font-weight: 600;
  line-height: 1.2;
}

.hero-role {
  flex-shrink: 0;
}

.hero-sub {
  margin-top: 4px;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

/* 元数据行：等距槽位 */
.hero-meta {
  display: flex;
  align-items: center;
  gap: 20px;
  margin-top: 14px;
}

.meta-item {
  display: flex;
  align-items: baseline;
  gap: 6px;
}

.meta-value {
  font-size: 22px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  color: var(--el-color-primary-dark-2);
}

.meta-label {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.meta-divider {
  width: 1px;
  height: 18px;
  background: var(--el-border-color-lighter);
}

.hero-actions {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 8px;
  flex-shrink: 0;
}

.hero-last {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

/* ============ 面板卡片 ============ */
.panel {
  border-radius: 10px;
}

.panel :deep(.el-card__header) {
  padding: 14px 20px;
  border-bottom: 1px solid var(--el-border-color-lighter);
}

.panel :deep(.el-card__body) {
  padding: 16px 20px;
}

.panel-header {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 600;
}

.panel-icon {
  color: var(--el-color-primary);
}

.count-badge {
  min-width: 20px;
  padding: 0 6px;
  height: 18px;
  line-height: 18px;
  border-radius: 9px;
  font-size: 11px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  text-align: center;
  color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
}

.ver-tag {
  flex-shrink: 0;
}

.header-spacer {
  flex: 1;
}

.search-input {
  width: 240px;
  flex-shrink: 0;
}

/* ============ 画像正文 ============ */
.profile-body {
  font-size: 14px;
  line-height: 1.75;
  color: var(--el-text-color-primary);
  word-break: break-word;
}

.profile-body :deep(h2) {
  margin: 16px 0 8px;
  padding-bottom: 6px;
  font-size: 15px;
  font-weight: 600;
  border-bottom: 1px solid var(--el-border-color-lighter);
}

.profile-body :deep(h2:first-child) {
  margin-top: 0;
}

.profile-body :deep(ul),
.profile-body :deep(p) {
  margin: 4px 0;
}

.profile-body :deep(li) {
  margin: 3px 0;
}

.profile-empty {
  padding: 12px 0;
}

/* ============ 类型筛选（分段器样式） ============ */
.type-filter {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 14px;
}

.type-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 5px 12px;
  border: 1px solid var(--el-border-color);
  border-radius: 14px;
  background: var(--el-bg-color);
  color: var(--el-text-color-regular);
  font-size: 13px;
  cursor: pointer;
  transition: all 0.2s;
}

.type-chip:hover {
  border-color: var(--el-color-primary-light-5);
  color: var(--el-color-primary);
}

.type-chip.active {
  border-color: var(--el-color-primary);
  color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
  font-weight: 600;
}

.chip-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--el-text-color-placeholder);
}

.chip-dot[data-type='fact'] { background: var(--el-color-primary); }
.chip-dot[data-type='preference'] { background: var(--el-color-success); }
.chip-dot[data-type='habit'] { background: var(--el-color-warning); }
.chip-dot[data-type='event'] { background: var(--el-color-info); }

.chip-count {
  font-size: 11px;
  font-variant-numeric: tabular-nums;
  color: var(--el-text-color-secondary);
}

/* ============ 记忆时间线（档案条目） ============ */
.memory-list {
  min-height: 200px;
}

.timeline {
  display: flex;
  flex-direction: column;
}

.memory-row {
  display: flex;
  gap: 14px;
  padding: 12px 4px;
  border-bottom: 1px dashed var(--el-border-color-lighter);
}

.memory-row:last-child {
  border-bottom: none;
}

.row-rail {
  position: relative;
  display: flex;
  justify-content: center;
  width: 12px;
  flex-shrink: 0;
}

.row-rail::before {
  content: '';
  position: absolute;
  top: 18px;
  bottom: -12px;
  width: 2px;
  background: var(--el-border-color-lighter);
}

.memory-row:last-child .row-rail::before {
  bottom: auto;
  height: 18px;
}

.row-dot {
  position: relative;
  z-index: 1;
  width: 10px;
  height: 10px;
  margin-top: 5px;
  border-radius: 50%;
  background: var(--el-text-color-placeholder);
  border: 2px solid var(--el-bg-color);
  box-shadow: 0 0 0 1px var(--el-border-color-lighter);
}

.row-dot[data-type='fact'] { background: var(--el-color-primary); }
.row-dot[data-type='preference'] { background: var(--el-color-success); }
.row-dot[data-type='habit'] { background: var(--el-color-warning); }
.row-dot[data-type='event'] { background: var(--el-color-info); }

.row-main {
  flex: 1;
  min-width: 0;
}

.row-content {
  font-size: 14px;
  line-height: 1.6;
  color: var(--el-text-color-primary);
}

/* 行内元数据：基线对齐 */
.row-meta {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 8px;
}

.type-tag {
  flex-shrink: 0;
}

/* 置信度：定宽轨道不抖动 */
.confidence-wrap {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 110px;
  flex-shrink: 0;
}

.confidence-track {
  flex: 1;
  height: 5px;
  border-radius: 3px;
  background: var(--el-fill-color-light);
  overflow: hidden;
}

.confidence-fill {
  height: 100%;
  border-radius: 3px;
  background: linear-gradient(90deg,
    var(--el-color-primary-light-5),
    var(--el-color-primary));
  transition: width 0.3s;
}

.confidence-num {
  width: 34px;
  font-size: 11px;
  font-variant-numeric: tabular-nums;
  text-align: right;
  color: var(--el-text-color-secondary);
}

.row-evidence {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  font-size: 12px;
  font-variant-numeric: tabular-nums;
  color: var(--el-text-color-secondary);
}

.row-evidence .el-icon {
  font-size: 12px;
}

.row-time {
  font-size: 12px;
  color: var(--el-text-color-placeholder);
}

.row-delete {
  flex-shrink: 0;
}

/* ============ 空状态 ============ */
.first-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 40px 16px 28px;
}

.first-empty-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 56px;
  height: 56px;
  margin-bottom: 14px;
  border-radius: 16px;
  color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
}

.first-empty-title {
  margin-bottom: 6px;
  font-size: 15px;
  font-weight: 600;
}

.first-empty-desc {
  max-width: 380px;
  font-size: 13px;
  text-align: center;
  color: var(--el-text-color-secondary);
}

/* ============ 分页 ============ */
.pagination {
  display: flex;
  justify-content: flex-end;
  margin-top: 12px;
}

/* ============ 响应式 ============ */
@media (max-width: 992px) {
  .hero {
    flex-direction: column;
    align-items: flex-start;
    gap: 14px;
  }

  .hero-actions {
    flex-direction: row;
    align-items: center;
    width: 100%;
    justify-content: space-between;
  }

  .body :deep(.el-col) {
    margin-bottom: 16px;
  }
}

@media (max-width: 640px) {
  .hero-meta {
    flex-wrap: wrap;
    gap: 12px;
  }

  .row-meta {
    flex-wrap: wrap;
  }

  .search-input {
    width: 100%;
  }

  .panel-header {
    flex-wrap: wrap;
  }
}
</style>
