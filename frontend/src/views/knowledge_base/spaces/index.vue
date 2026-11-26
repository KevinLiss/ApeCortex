<template>
  <div class="kb-spaces-page">
    <!-- ═══════════ 列表视图 ═══════════ -->
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

      <!-- 我的空间 -->
      <div class="group-title">{{ t('knowledge.spaces.owned') }}</div>
      <el-row :gutter="16">
        <el-col v-for="s in ownedSpaces" :key="s.id" :xs="24" :sm="12" :md="8" :lg="6">
          <el-card shadow="hover" class="space-card" @click="openSpace(s)">
            <div class="space-head">
              <div class="space-icon" :class="{ shared: s.visibility === 'shared' }">
                <el-icon :size="20"><Folder /></el-icon>
              </div>
              <div class="space-meta">
                <div class="space-name">{{ s.name }}</div>
                <el-tag size="small" :type="s.visibility === 'shared' ? 'warning' : 'info'" effect="plain" round>
                  {{ s.visibility === 'shared' ? t('knowledge.spaces.sharedTag') : t('knowledge.spaces.privateTag') }}
                </el-tag>
              </div>
            </div>
            <div class="space-desc">{{ s.description || '—' }}</div>
            <div class="space-footer">
              <span class="stat"><el-icon><Document /></el-icon>{{ s.page_count }} {{ t('knowledge.spaces.pages') }}</span>
              <span class="stat"><el-icon><Right /></el-icon></span>
            </div>
          </el-card>
        </el-col>
        <el-col :xs="24" :sm="12" :md="8" :lg="6" v-if="!ownedSpaces.length">
          <el-card shadow="never" class="empty-card">
            <div class="empty-title">{{ t('knowledge.spaces.empty') }}</div>
            <div class="empty-desc">{{ t('knowledge.spaces.emptyDesc') }}</div>
          </el-card>
        </el-col>
      </el-row>

      <!-- 共享空间（他人共享给我） -->
      <div class="group-title" v-if="sharedSpaces.length">{{ t('knowledge.spaces.sharedGroup') }}</div>
      <el-row :gutter="16" v-if="sharedSpaces.length">
        <el-col v-for="s in sharedSpaces" :key="s.id" :xs="24" :sm="12" :md="8" :lg="6">
          <el-card shadow="hover" class="space-card" @click="openSpace(s)">
            <div class="space-head">
              <div class="space-icon shared"><el-icon :size="20"><User /></el-icon></div>
              <div class="space-meta">
                <div class="space-name">{{ s.name }}</div>
                <el-tag size="small" type="warning" effect="plain" round>
                  {{ s.my_role === 'writer' ? t('knowledge.spaces.roleWriter') : t('knowledge.spaces.roleReader') }}
                </el-tag>
              </div>
            </div>
            <div class="space-desc">{{ s.description || '—' }}</div>
            <div class="space-footer">
              <span class="stat"><el-icon><Document /></el-icon>{{ s.page_count }} {{ t('knowledge.spaces.pages') }}</span>
              <span class="stat"><el-icon><Right /></el-icon></span>
            </div>
          </el-card>
        </el-col>
      </el-row>

      <!-- 公开空间 -->
      <div class="group-title" v-if="publicSpaces.length">{{ t('knowledge.spaces.publicGroup') }}</div>
      <el-row :gutter="16" v-if="publicSpaces.length">
        <el-col v-for="s in publicSpaces" :key="s.id" :xs="24" :sm="12" :md="8" :lg="6">
          <el-card shadow="hover" class="space-card" @click="openSpace(s)">
            <div class="space-head">
              <div class="space-icon public"><el-icon :size="20"><Collection /></el-icon></div>
              <div class="space-meta">
                <div class="space-name">{{ s.name }}</div>
                <el-tag size="small" type="success" effect="plain" round>{{ t('knowledge.spaces.publicTag') }}</el-tag>
              </div>
            </div>
            <div class="space-desc">{{ s.description || '—' }}</div>
            <div class="space-footer">
              <span class="stat"><el-icon><Document /></el-icon>{{ s.page_count }} {{ t('knowledge.spaces.pages') }}</span>
              <span class="stat"><el-icon><Right /></el-icon></span>
            </div>
          </el-card>
        </el-col>
      </el-row>

      <!-- 新建空间弹窗 -->
      <el-dialog v-model="createVisible" :title="t('knowledge.spaces.create')" width="420px">
        <el-form label-position="top">
          <el-form-item :label="t('knowledge.spaces.name')">
            <el-input v-model="newName" :placeholder="t('knowledge.spaces.namePlaceholder')" maxlength="100" />
          </el-form-item>
          <el-form-item :label="t('knowledge.spaces.desc')">
            <el-input v-model="newDesc" type="textarea" :rows="2" :placeholder="t('knowledge.spaces.descPlaceholder')" maxlength="500" />
          </el-form-item>
          <el-form-item v-if="canCreatePublic" :label="t('knowledge.spaces.visibility')">
            <el-radio-group v-model="newVisibility">
              <el-radio value="private">{{ t('knowledge.spaces.visibilityPrivate') }}</el-radio>
              <el-radio value="shared">{{ t('knowledge.spaces.visibilityShared') }}</el-radio>
              <el-radio value="public">{{ t('knowledge.spaces.visibilityPublic') }}</el-radio>
            </el-radio-group>
            <div v-if="newVisibility === 'public'" class="visibility-hint">
              {{ t('knowledge.spaces.visibilityPublicHint') }}
            </div>
          </el-form-item>
        </el-form>
        <template #footer>
          <el-button @click="createVisible = false">{{ t('common.action.cancel') }}</el-button>
          <el-button type="primary" :loading="creating" @click="handleCreate">{{ t('common.action.confirm') }}</el-button>
        </template>
      </el-dialog>
    </template>

    <!-- ═══════════ 详情视图 ═══════════ -->
    <template v-else>
      <div class="detail-header">
        <el-button :icon="ArrowLeft" text @click="backToList">{{ t('knowledge.spaces.backToList') }}</el-button>
        <div class="detail-title-wrap">
          <div class="space-icon" :class="{ public: currentSpace.visibility === 'public', shared: currentSpace.visibility === 'shared' }">
            <el-icon :size="20"><Folder /></el-icon>
          </div>
          <div class="detail-title-meta">
            <div class="detail-title-line">
              <h2>{{ currentSpace.name }}</h2>
              <el-tag size="small" :type="currentSpace.visibility === 'public' ? 'success' : currentSpace.visibility === 'shared' ? 'warning' : 'info'" effect="plain" round>
                {{ visibilityLabel(currentSpace.visibility) }}
              </el-tag>
            </div>
            <p class="detail-desc">{{ currentSpace.description || '—' }}</p>
          </div>
        </div>
        <div class="detail-stats">
          <div class="stat-block">
            <span class="stat-num">{{ spaceSummary?.doc_count ?? currentSpace.doc_count ?? 0 }}</span>
            <span class="stat-label">{{ t('knowledge.spaces.docsUnit') }}</span>
          </div>
          <div class="stat-block">
            <span class="stat-num">{{ spaceSummary?.page_count ?? currentSpace.page_count ?? 0 }}</span>
            <span class="stat-label">{{ t('knowledge.spaces.pages') }}</span>
          </div>
        </div>
      </div>

      <el-tabs v-model="activeTab" class="detail-tabs">
        <el-tab-pane :label="t('knowledge.spaces.tabDocs')" name="docs">
          <DocsTab
            v-if="activeTab === 'docs'"
            :space-id="currentSpace.id"
            :can-write="canWrite"
            @refresh="refreshAll"
            @goto-page="gotoPagesTab"
          />
        </el-tab-pane>
        <el-tab-pane :label="t('knowledge.spaces.tabPages')" name="pages">
          <PagesTab
            v-if="activeTab === 'pages'"
            ref="pagesTabRef"
            :space-id="currentSpace.id"
            :can-edit="canWrite"
            :can-publish="canWrite"
            :can-promote="currentSpace.my_role === 'owner' && currentSpace.visibility !== 'public'"
            @refresh="refreshAll"
          />
        </el-tab-pane>
        <el-tab-pane v-if="currentSpace.visibility === 'shared'" :label="t('knowledge.spaces.tabMembers')" name="members">
          <MembersTab v-if="activeTab === 'members'" :space-id="currentSpace.id" :can-manage="currentSpace.my_role === 'owner'" />
        </el-tab-pane>
      </el-tabs>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { ArrowLeft, Collection, Document, Folder, Plus, Right, User } from '@element-plus/icons-vue'

import { createKbSpace, getKbSpaceDocs, getKbSpaces } from '@/api'
import { useUserStore } from '@/stores/user'
import DocsTab from './components/DocsTab.vue'
import MembersTab from './components/MembersTab.vue'
import PagesTab from './components/PagesTab.vue'

const { t } = useI18n()
const route = useRoute()
const router = useRouter()
const userStore = useUserStore()

const spaces = ref<any[]>([])
const createVisible = ref(false)
const newName = ref('')
const newDesc = ref('')
const newVisibility = ref('private')
const creating = ref(false)
const activeTab = ref('docs')
const spaceSummary = ref<any>(null)
const pagesTabRef = ref<any>(null)

const ownedSpaces = computed(() => spaces.value.filter((s) => s.owned && s.visibility !== 'public'))
const sharedSpaces = computed(() => spaces.value.filter((s) => !s.owned && s.visibility === 'shared'))
const publicSpaces = computed(() => spaces.value.filter((s) => s.visibility === 'public'))
const canCreatePublic = computed(() => userStore.hasPermission('knowledge_base:promotion:review'))

const currentSpaceId = computed(() => Number(route.query.space || 0) || null)
const currentSpace = computed(() =>
  currentSpaceId.value ? spaces.value.find((s) => s.id === currentSpaceId.value) ?? null : null,
)
const canWrite = computed(() => ['owner', 'writer'].includes(currentSpace.value?.my_role))

function visibilityLabel(v: string): string {
  const map: Record<string, string> = {
    private: t('knowledge.spaces.privateTag'),
    shared: t('knowledge.spaces.sharedTag'),
    public: t('knowledge.spaces.publicTag'),
  }
  return map[v] ?? v
}

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
  // 等 PagesTab 挂载后跳到指定词条
  setTimeout(() => pagesTabRef.value?.gotoPage(pageId), 50)
}

async function handleCreate() {
  if (!newName.value.trim()) return
  creating.value = true
  try {
    await createKbSpace({ name: newName.value.trim(), description: newDesc.value.trim(), visibility: newVisibility.value })
    ElMessage.success(t('knowledge.spaces.created'))
    createVisible.value = false
    newName.value = ''
    newDesc.value = ''
    newVisibility.value = 'private'
    fetchSpaces()
  } catch (e: any) {
    /* 错误提示由 request 拦截器统一展示（含可读化文案） */
  } finally {
    creating.value = false
  }
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
  margin-bottom: 20px;
}

.page-header h2 {
  margin: 0 0 4px;
  font-size: 20px;
}

.group-title {
  margin: 8px 0 12px;
  font-size: 14px;
  font-weight: 600;
  color: var(--el-text-color-secondary);
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

.space-icon.shared {
  color: var(--el-color-warning);
  background: var(--el-color-warning-light-9);
}

.space-icon.public {
  color: var(--el-color-success);
  background: var(--el-color-success-light-9);
}

.space-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
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

.visibility-hint {
  margin-top: 6px;
  font-size: 12px;
  line-height: 1.5;
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
  gap: 24px;
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
