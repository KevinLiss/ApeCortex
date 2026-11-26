<template>
  <div class="kb-spaces-page">
    <!-- ═══════════ 列表视图（标签筛选，Dify 式）═══════════ -->
    <template v-if="!currentSpace">
      <div class="page-header">
        <div>
          <h2>{{ t('knowledge.spaces.title') }}</h2>
          <p class="text-muted">{{ t('knowledge.spaces.subtitle') }}</p>
        </div>
        <el-button type="primary" @click="createVisible = true" v-permission="'knowledge_base:space:create'">
          <el-icon><Plus /></el-icon>
          <span>{{ t('knowledge.spaces.create') }}</span>
        </el-button>
      </div>

      <!-- 筛选标签：全部 / 我创建的（Dify 知识库列表同款交互） -->
      <div class="filter-tabs">
        <button
          v-for="ft in filterTabs"
          :key="ft.key"
          class="filter-tab"
          :class="{ active: filter === ft.key }"
          @click="filter = ft.key"
        >{{ ft.label }}</button>
      </div>

      <!-- 空间卡片网格 -->
      <el-row :gutter="16">
        <el-col v-for="s in filteredSpaces" :key="s.id" :xs="24" :sm="12" :md="8" :lg="6">
          <el-card shadow="hover" class="space-card" @click="openSpace(s)">
            <div class="space-head">
              <div class="space-icon">
                <el-icon :size="20"><Folder /></el-icon>
              </div>
              <div class="space-meta">
                <div class="space-name">{{ s.name }}</div>
              </div>
              <el-dropdown trigger="click" class="space-more" @command="(cmd: string) => onSpaceCmd(cmd, s)">
                <span class="space-more-btn" title="更多操作" @click.stop>
                  <el-icon :size="16"><MoreFilled /></el-icon>
                </span>
                <template #dropdown>
                  <el-dropdown-menu>
                    <el-dropdown-item command="edit">{{ t('knowledge.spaces.rename') }}</el-dropdown-item>
                    <el-dropdown-item command="delete" divided class="danger-item">
                      {{ t('knowledge.spaces.remove') }}
                    </el-dropdown-item>
                  </el-dropdown-menu>
                </template>
              </el-dropdown>
            </div>
            <div class="space-desc">{{ s.description || '—' }}</div>
            <div class="space-footer">
              <span class="stat"><el-icon><Document /></el-icon>{{ s.page_count }} {{ t('knowledge.spaces.pages') }}</span>
              <span class="stat"><el-icon><Right /></el-icon></span>
            </div>
          </el-card>
        </el-col>
        <el-col :xs="24" :sm="12" :md="8" :lg="6" v-if="!filteredSpaces.length">
          <el-card shadow="never" class="empty-card">
            <div class="empty-title">{{ t('knowledge.spaces.empty') }}</div>
            <div class="empty-desc">{{ t('knowledge.spaces.emptyDesc') }}</div>
          </el-card>
        </el-col>
      </el-row>

      <!-- 新建空间弹窗（个人知识库，无类型概念；复用于编辑模式） -->
      <el-dialog v-model="createVisible" :title="t('knowledge.spaces.create')" width="420px">
        <el-form label-position="top">
          <el-form-item :label="t('knowledge.spaces.name')">
            <el-input v-model="newName" :placeholder="t('knowledge.spaces.namePlaceholder')" maxlength="100" />
          </el-form-item>
          <el-form-item :label="t('knowledge.spaces.desc')">
            <el-input v-model="newDesc" type="textarea" :rows="2" :placeholder="t('knowledge.spaces.descPlaceholder')" maxlength="500" />
          </el-form-item>
        </el-form>
        <template #footer>
          <el-button @click="createVisible = false">{{ t('common.action.cancel') }}</el-button>
          <el-button type="primary" :loading="creating" @click="handleCreate">{{ t('common.action.confirm') }}</el-button>
        </template>
      </el-dialog>

      <!-- 编辑空间弹窗 -->
      <el-dialog v-model="editVisible" :title="t('knowledge.spaces.editTitle')" width="420px">
        <el-form label-position="top">
          <el-form-item :label="t('knowledge.spaces.name')">
            <el-input v-model="editName" :placeholder="t('knowledge.spaces.namePlaceholder')" maxlength="100" />
          </el-form-item>
          <el-form-item :label="t('knowledge.spaces.desc')">
            <el-input v-model="editDesc" type="textarea" :rows="2" :placeholder="t('knowledge.spaces.descPlaceholder')" maxlength="500" />
          </el-form-item>
        </el-form>
        <template #footer>
          <el-button @click="editVisible = false">{{ t('common.action.cancel') }}</el-button>
          <el-button type="primary" :loading="saving" @click="handleSave">{{ t('common.action.confirm') }}</el-button>
        </template>
      </el-dialog>
    </template>

    <!-- ═══════════ 详情视图 ═══════════ -->
    <template v-else>
      <div class="detail-header">
        <el-button :icon="ArrowLeft" text @click="backToList">{{ t('knowledge.spaces.backToList') }}</el-button>
        <div class="detail-title-wrap">
          <div class="space-icon">
            <el-icon :size="20"><Folder /></el-icon>
          </div>
          <div class="detail-title-meta">
            <div class="detail-title-line">
              <h2>{{ currentSpace.name }}</h2>
            </div>
            <p class="detail-desc">{{ currentSpace.description || '—' }}</p>
          </div>
        </div>
        <div class="detail-stats">
          <div class="stat-block">
            <span class="stat-num">{{ spaceSummary?.doc_count ?? 0 }}</span>
            <span class="stat-label">{{ t('knowledge.spaces.docsUnit') }}</span>
          </div>
          <div class="stat-block">
            <span class="stat-num">{{ spaceSummary?.page_count ?? 0 }}</span>
            <span class="stat-label">{{ t('knowledge.spaces.pages') }}</span>
          </div>
          <div class="detail-actions">
            <el-button text :icon="EditPen" @click="openEdit(currentSpace)">
              {{ t('knowledge.spaces.rename') }}
            </el-button>
            <el-button text type="danger" :icon="Delete" @click="confirmDelete(currentSpace)">
              {{ t('knowledge.spaces.remove') }}
            </el-button>
          </div>
        </div>
      </div>

      <el-tabs v-model="activeTab" class="detail-tabs">
        <el-tab-pane :label="t('knowledge.spaces.tabDocs')" name="docs">
          <DocsTab
            v-if="activeTab === 'docs'"
            :space-id="currentSpace.id"
            :can-write="true"
            @refresh="refreshAll"
            @goto-page="gotoPagesTab"
          />
        </el-tab-pane>
        <el-tab-pane :label="t('knowledge.spaces.tabPages')" name="pages">
          <PagesTab
            v-if="activeTab === 'pages'"
            ref="pagesTabRef"
            :space-id="currentSpace.id"
            :can-edit="true"
            :can-publish="false"
            :can-push="true"
            @refresh="refreshAll"
          />
        </el-tab-pane>
      </el-tabs>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ArrowLeft, Delete, Document, EditPen, Folder, MoreFilled, Plus, Right } from '@element-plus/icons-vue'

import { createKbSpace, deleteKbSpace, getKbSpaceDocs, getKbSpaces, updateKbSpace } from '@/api'
import DocsTab from './components/DocsTab.vue'
import PagesTab from './components/PagesTab.vue'

const { t } = useI18n()
const route = useRoute()
const router = useRouter()

const spaces = ref<any[]>([])
const createVisible = ref(false)
const newName = ref('')
const newDesc = ref('')
const creating = ref(false)
const editVisible = ref(false)
const editName = ref('')
const editDesc = ref('')
const editTarget = ref<any>(null)
const saving = ref(false)
const activeTab = ref('docs')
const spaceSummary = ref<any>(null)
const pagesTabRef = ref<any>(null)
const filter = ref('all')

const filterTabs = computed(() => [
  { key: 'all', label: t('knowledge.spaces.filterAll') },
  { key: 'recent', label: t('knowledge.spaces.filterRecent') },
])

const filteredSpaces = computed(() => {
  if (filter.value === 'recent') return [...spaces.value].reverse()
  return spaces.value
})

const currentSpaceId = computed(() => Number(route.query.space || 0) || null)
const currentSpace = computed(() =>
  currentSpaceId.value ? spaces.value.find((s) => s.id === currentSpaceId.value) ?? null : null,
)

async function fetchSpaces() {
  try {
    const res: any = await getKbSpaces()
    spaces.value = res?.items ?? []
  } catch { /* 拦截器已提示 */ }
}

async function fetchSummary() {
  if (!currentSpaceId.value) return
  try {
    const res: any = await getKbSpaceDocs(currentSpaceId.value)
    spaceSummary.value = res?.space ?? null
  } catch { /* 拦截器已提示 */ }
}

function openSpace(s: any) {
  router.push({ query: { ...route.query, space: s.id } })
}

function backToList() {
  router.push({ query: { ...route.query, space: undefined } })
}

async function refreshAll() {
  await Promise.all([fetchSpaces(), fetchSummary()])
}

function gotoPagesTab(pageId: number) {
  activeTab.value = 'pages'
  setTimeout(() => pagesTabRef.value?.gotoPage(pageId), 50)
}

async function handleCreate() {
  if (!newName.value.trim()) return
  creating.value = true
  try {
    await createKbSpace({ name: newName.value.trim(), description: newDesc.value.trim() })
    ElMessage.success(t('knowledge.spaces.created'))
    createVisible.value = false
    newName.value = ''
    newDesc.value = ''
    fetchSpaces()
  } catch { /* 拦截器已提示 */ } finally {
    creating.value = false
  }
}

function onSpaceCmd(cmd: string, s: any) {
  if (cmd === 'edit') openEdit(s)
  else if (cmd === 'delete') confirmDelete(s)
}

function openEdit(s: any) {
  editTarget.value = s
  editName.value = s.name
  editDesc.value = s.description || ''
  editVisible.value = true
}

async function handleSave() {
  if (!editTarget.value || !editName.value.trim()) return
  saving.value = true
  try {
    await updateKbSpace(editTarget.value.id, {
      name: editName.value.trim(), description: editDesc.value.trim(),
    })
    ElMessage.success(t('knowledge.spaces.updated'))
    editVisible.value = false
    fetchSpaces()
  } catch { /* 拦截器已提示 */ } finally {
    saving.value = false
  }
}

async function confirmDelete(s: any) {
  try {
    await ElMessageBox.confirm(
      t('knowledge.spaces.removeConfirm', { name: s.name }),
      t('knowledge.spaces.removeConfirmTitle'),
      { type: 'warning', confirmButtonText: t('common.action.delete'), cancelButtonText: t('common.action.cancel') },
    )
  } catch { /* 取消 */ return }
  try {
    await deleteKbSpace(s.id)
    ElMessage.success(t('knowledge.spaces.removed'))
    // 若删除的是当前打开的空间 → 回列表
    if (currentSpaceId.value === s.id) backToList()
    fetchSpaces()
  } catch { /* 拦截器已提示（hub 拒删等） */ }
}

watch(currentSpaceId, () => {
  activeTab.value = 'docs'
  spaceSummary.value = null
  if (currentSpaceId.value) fetchSummary()
})

onMounted(() => {
  fetchSpaces()
  if (currentSpaceId.value) fetchSummary()
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

/* 筛选标签（Dify 式分段） */
.filter-tabs {
  display: flex;
  gap: 8px;
  margin-bottom: 16px;
}

.filter-tab {
  padding: 6px 16px;
  border: 1px solid var(--el-border-color);
  border-radius: 16px;
  background: var(--el-bg-color);
  color: var(--el-text-color-regular);
  font-size: 13px;
  cursor: pointer;
  transition: all 0.2s;
}

.filter-tab:hover {
  border-color: var(--el-color-primary-light-5);
}

.filter-tab.active {
  border-color: var(--el-color-primary);
  color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
  font-weight: 600;
}

.space-card {
  margin-bottom: 16px;
  border-radius: 10px;
  cursor: pointer;
  transition: transform 0.15s;
}

.space-card:hover {
  transform: translateY(-2px);
}

.space-head {
  display: flex;
  align-items: center;
  gap: 12px;
}

.space-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  width: 40px;
  height: 40px;
  border-radius: 10px;
  color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
}

.space-meta {
  min-width: 0;
  flex: 1;
}

/* 卡片右上角更多操作（悬停可见，避免误触） */
.space-more {
  flex-shrink: 0;
  opacity: 0;
  transition: opacity 0.2s;
}

.space-card:hover .space-more {
  opacity: 1;
}

.space-more-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  border-radius: 6px;
  color: var(--el-text-color-secondary);
  cursor: pointer;
}

.space-more-btn:hover {
  color: var(--el-color-primary);
  background: var(--el-fill-color-light);
}

.danger-item {
  color: var(--el-color-danger);
}

.space-name {
  font-size: 15px;
  font-weight: 600;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.space-desc {
  height: 40px;
  margin-top: 10px;
  font-size: 13px;
  color: var(--el-text-color-secondary);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.space-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 12px;
}

.stat {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 13px;
  font-variant-numeric: tabular-nums;
  color: var(--el-text-color-secondary);
}

.empty-card {
  margin-bottom: 16px;
  border-radius: 10px;
  border-style: dashed;
}

.empty-title {
  font-weight: 600;
  font-size: 14px;
}

.empty-desc {
  margin-top: 6px;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

/* 详情视图 */
.detail-header {
  display: flex;
  align-items: center;
  gap: 14px;
  margin-bottom: 18px;
}

.detail-title-wrap {
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
  flex: 1;
}

.detail-title-meta {
  min-width: 0;
}

.detail-title-line {
  display: flex;
  align-items: center;
  gap: 10px;
}

.detail-title-line h2 {
  margin: 0;
  font-size: 18px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.detail-desc {
  margin: 2px 0 0;
  font-size: 13px;
  color: var(--el-text-color-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.detail-stats {
  display: flex;
  align-items: center;
  gap: 24px;
}

.detail-actions {
  display: flex;
  gap: 4px;
  margin-left: 8px;
}

.stat-block {
  display: flex;
  flex-direction: column;
  align-items: center;
}

.stat-num {
  font-size: 20px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  color: var(--el-color-primary);
}

.stat-label {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.detail-tabs :deep(.el-tabs__header) {
  margin-bottom: 14px;
}
</style>
