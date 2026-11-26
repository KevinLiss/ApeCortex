<template>
  <div class="kb-hub-page">
    <!-- 页头 -->
    <div class="page-header">
      <div>
        <h2>{{ t('knowledge.hub.title') }}</h2>
        <p class="text-muted">{{ t('knowledge.hub.subtitle') }}</p>
      </div>
      <div class="header-actions">
        <el-input
          v-model="searchText"
          class="hub-search"
          :prefix-icon="Search"
          :placeholder="t('knowledge.hub.searchPlaceholder')"
          clearable
          @clear="fetchPages"
          @keyup.enter="fetchPages"
        />
        <el-button
          v-if="canManageCategory"
          :icon="Folder"
          @click="openCatManage"
        >{{ t('knowledge.hub.catManageTitle') }}</el-button>
      </div>
    </div>

    <el-row :gutter="16">
      <!-- 左：分类树（按分类视图） -->
      <el-col :xs="24" :md="7" :lg="6">
        <el-card shadow="never" class="cat-panel">
          <div class="cat-scroll">
            <div class="cat-node" :class="{ active: selectedCatId === null }" @click="selectCat(null)">
              <el-icon><Collection /></el-icon>
              <span class="cat-name">{{ t('knowledge.hub.allCategories') }}</span>
                          </div>
            <template v-for="cat in categories" :key="cat.id">
              <div class="cat-node" :class="{ active: selectedCatId === cat.id }" @click="selectCat(cat.id)">
                <el-icon><component :is="cat.icon || 'Folder'" /></el-icon>
                <span class="cat-name">{{ cat.name }}</span>
                <span class="cat-count">{{ cat.space_count ?? 0 }}</span>
              </div>
              <div
                v-for="child in cat.children"
                :key="child.id"
                class="cat-node child"
                :class="{ active: selectedCatId === child.id }"
                @click="selectCat(child.id)"
              >
                <span class="cat-dot" />
                <span class="cat-name">{{ child.name }}</span>
                <span class="cat-count">{{ child.space_count ?? 0 }}</span>
              </div>
            </template>
            <div class="cat-node" :class="{ active: selectedCatId === 0 }" @click="selectCat(0)">
              <el-icon><Folder /></el-icon>
              <span class="cat-name">{{ t('knowledge.hub.uncategorized') }}</span>
            </div>
          </div>
        </el-card>
      </el-col>

      <!-- 右：内容区 -->
      <el-col :xs="24" :md="17" :lg="18">
        <!-- 词条列表 -->
        <el-card shadow="never" class="pages-panel">
          <div v-loading="pagesLoading" class="pages-list">
            <div v-for="p in pages" :key="p.id" class="page-row" @click="openPage(p)">
              <div class="row-title-line">
                <span class="row-title">{{ p.title }}</span>
                <span class="row-conf">{{ Math.round((p.confidence ?? 0) * 100) }}%</span>
              </div>
              <div class="row-meta">
                <span v-if="categoryName(p.category_id)" class="meta-lib">
                  {{ categoryName(p.category_id) }}
                </span>
                <span v-if="p.updated_at">{{ t('knowledge.hub.updated') }} {{ formatTime(p.updated_at) }}</span>
              </div>
            </div>
            <el-empty
              v-if="!pages.length && !pagesLoading"
              :description="t('knowledge.hub.empty')"
              :image-size="80"
            />
          </div>
          <div class="pagination" v-if="total > pageSize">
            <el-pagination
              v-model:current-page="pageNum"
              :total="total"
              :page-size="pageSize"
              layout="prev, pager, next"
              small
              background
              @current-change="fetchPages"
            />
          </div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 词条阅读抽屉 -->
    <el-drawer
      v-model="drawerVisible"
      :title="pageDetail?.title || t('knowledge.hub.title')"
      size="52%"
      :z-index="2000"
      class="kb-doc-drawer"
    >
      <div v-if="pageDetail" class="drawer-body">
        <div class="drawer-meta">
          <el-tag v-if="categoryName(pageDetail.category_id)" size="small" type="primary" effect="plain" round>
            {{ categoryName(pageDetail.category_id) }}
          </el-tag>
          <span v-if="pageDetail.updated_at">{{ t('knowledge.hub.updated') }} {{ formatTime(pageDetail.updated_at) }}</span>
        </div>
        <div class="page-body" v-html="renderedBody" />
        <div v-if="(pageDetail.out_links || []).length" class="outlinks-box">
          <div class="refs-title">{{ t('knowledge.hub.outLinks') }}</div>
          <el-tag
            v-for="slug in pageDetail.out_links"
            :key="slug"
            size="small"
            effect="plain"
            class="outlink-tag"
          >{{ slug }}</el-tag>
        </div>
        <div v-if="(pageDetail.source_refs || []).length" class="refs-box">
          <div class="refs-title">{{ t('knowledge.hub.sources') }}</div>
          <div v-for="(r, i) in pageDetail.source_refs" :key="i" class="ref-item">
            {{ i + 1 }}. {{ r.title }}
            <a v-if="r.url" :href="r.url" target="_blank" rel="noopener">{{ r.url }}</a>
          </div>
        </div>
      </div>
    </el-drawer>

    <!-- 分类管理弹窗 -->
    <el-dialog v-model="catManageVisible" :title="t('knowledge.hub.catManageTitle')" width="640px" top="6vh">
      <div class="cat-add">
        <el-input v-model="newCatName" style="width: 200px" :placeholder="t('knowledge.hub.catName')" maxlength="50" />
        <el-select v-model="newCatParent" style="width: 160px" clearable>
          <el-option v-for="c in rootCategories" :key="c.id" :value="c.id" :label="c.name" />
        </el-select>
        <el-input v-model="newCatIcon" style="width: 140px" :placeholder="t('knowledge.hub.catIcon')" />
        <el-input-number v-model="newCatSort" :min="0" style="width: 110px" />
        <el-button type="primary" @click="handleCreateCat">{{ t('knowledge.hub.catAdd') }}</el-button>
      </div>
      <el-table :data="flatCategories" size="small" max-height="360">
        <el-table-column prop="name" :label="t('knowledge.hub.catName')" min-width="180">
          <template #default="{ row }">
            <span v-if="row.parent_id" class="cat-indent">└ </span>{{ row.name }}
          </template>
        </el-table-column>
        <el-table-column prop="icon" :label="t('knowledge.hub.catIcon')" width="110" />
        <el-table-column prop="sort" :label="t('knowledge.hub.catSort')" width="70" />
        <el-table-column :label="t('knowledge.hub.colActions')" width="130" align="center">
          <template #default="{ row }">
            <el-button link size="small" @click="startEditCat(row)">{{ t('common.action.edit') }}</el-button>
            <el-button link size="small" type="danger" @click="handleDeleteCat(row)">
              {{ t('common.action.delete') }}
            </el-button>
          </template>
        </el-table-column>
      </el-table>
      <!-- 编辑行 -->
      <div v-if="editingCat" class="cat-edit-row">
        <el-input v-model="editingCat.name" style="width: 200px" maxlength="50" />
        <el-input v-model="editingCat.icon" style="width: 140px" />
        <el-input-number v-model="editingCat.sort" :min="0" style="width: 110px" />
        <el-button type="primary" size="small" @click="handleSaveCat">{{ t('common.action.confirm') }}</el-button>
        <el-button size="small" @click="editingCat = null">{{ t('common.action.cancel') }}</el-button>
      </div>
      <p class="cat-tip">{{ t('knowledge.hub.catTip') }}</p>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ArrowLeft, Collection, Folder, Search } from '@element-plus/icons-vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'

import {
  createKbCategory, deleteKbCategory, getKbCategories, getKbHubPage, getKbHubPages, updateKbCategory,
} from '@/api'
import { useUserStore } from '@/stores/user'

const { t } = useI18n()
const userStore = useUserStore()

const searchText = ref('')
const categories = ref<any[]>([])
const selectedCatId = ref<number | null>(null)

const pages = ref<any[]>([])
const total = ref(0)
const pageNum = ref(1)
const pageSize = 20
const pagesLoading = ref(false)

const drawerVisible = ref(false)
const pageDetail = ref<any>(null)

const catManageVisible = ref(false)
const newCatName = ref('')
const newCatParent = ref<number | null>(null)
const newCatIcon = ref('Folder')
const newCatSort = ref(0)
const editingCat = ref<any>(null)

const canManageCategory = computed(() => userStore.hasPermission('knowledge_base:category:manage'))
const rootCategories = computed(() => categories.value)
const flatCategories = computed(() =>
  categories.value.flatMap((c) => [c, ...(c.children || [])].map(({ children, ...rest }: any) => rest)),
)

const renderedBody = computed(() => {
  if (!pageDetail.value?.body_md) return ''
  try {
    return DOMPurify.sanitize(marked.parse(pageDetail.value.body_md) as string)
  } catch {
    return pageDetail.value.body_md
  }
})


function categoryName(categoryId: number | null): string {
  for (const c of categories.value) {
    if (c.id === categoryId) return c.name
    const child = (c.children || []).find((ch: any) => ch.id === categoryId)
    if (child) return `${c.name} / ${child.name}`
  }
  return ''
}

function formatTime(iso: string): string {
  return (iso || '').slice(0, 10)
}

async function fetchCategories() {
  try {
    const res: any = await getKbCategories()
    categories.value = res?.items ?? []
  } catch { /* 拦截器已提示 */ }
}

async function fetchPages() {
  pagesLoading.value = true
  try {
    const params: any = { page: pageNum.value, page_size: pageSize }
    if (searchText.value.trim()) params.search = searchText.value.trim()
    if (selectedCatId.value !== null) {
      params.category_id = selectedCatId.value
    }
    const res: any = await getKbHubPages(params)
    pages.value = res?.items ?? []
    total.value = res?.total ?? 0
  } catch { /* 拦截器已提示 */ } finally {
    pagesLoading.value = false
  }
}

function selectCat(catId: number | null) {
  selectedCatId.value = catId
  pageNum.value = 1
  fetchPages()
}

async function openPage(p: any) {
  drawerVisible.value = true
  pageDetail.value = p // 先展示标题骨架
  try {
    pageDetail.value = await getKbHubPage(p.id)
  } catch {
    drawerVisible.value = false
  }
}

// ---- 分类管理 ----
function openCatManage() {
  catManageVisible.value = true
  newCatName.value = ''
  newCatParent.value = null
  newCatIcon.value = 'Folder'
  newCatSort.value = 0
  editingCat.value = null
}

async function handleCreateCat() {
  if (!newCatName.value.trim()) return
  try {
    await createKbCategory({
      name: newCatName.value.trim(),
      parent_id: newCatParent.value ?? undefined,
      icon: newCatIcon.value.trim() || 'Folder',
      sort: newCatSort.value,
    })
    ElMessage.success(t('knowledge.hub.catSaved'))
    newCatName.value = ''
    await fetchCategories()
  } catch { /* 拦截器已提示 */ }
}

function startEditCat(row: any) {
  editingCat.value = { ...row }
}

async function handleSaveCat() {
  if (!editingCat.value?.name?.trim()) return
  try {
    await updateKbCategory(editingCat.value.id, {
      name: editingCat.value.name.trim(),
      icon: editingCat.value.icon,
      sort: editingCat.value.sort,
    })
    ElMessage.success(t('knowledge.hub.catSaved'))
    editingCat.value = null
    await fetchCategories()
  } catch { /* 拦截器已提示 */ }
}

async function handleDeleteCat(row: any) {
  try {
    await ElMessageBox.confirm(t('knowledge.hub.catDeleteConfirm'), { type: 'warning' })
  } catch { return }
  try {
    await deleteKbCategory(row.id)
    ElMessage.success(t('knowledge.hub.catDeleted'))
    if (selectedCatId.value === row.id) selectedCatId.value = null
    await fetchCategories()
    fetchPages()
  } catch { /* 拦截器已提示 */ }
}

watch(selectedCatId, () => { pageNum.value = 1 })

onMounted(async () => {
  await fetchCategories()
  fetchPages()
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
  gap: 10px;
}

.hub-search {
  width: 260px;
}

.view-toggle {
  margin-bottom: 14px;
}

/* 分类树 */
.cat-panel {
  border-radius: 10px;
}

.cat-panel :deep(.el-card__body) {
  padding: 8px;
}

.cat-scroll {
  max-height: 560px;
  overflow-y: auto;
}

.cat-node {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 9px 10px;
  border-radius: 8px;
  font-size: 14px;
  color: var(--el-text-color-regular);
  cursor: pointer;
  transition: background 0.15s;
}

.cat-node:hover {
  background: var(--el-fill-color-light);
}

.cat-node.active {
  background: var(--el-color-primary-light-9);
  color: var(--el-color-primary);
  font-weight: 600;
}

.cat-node.child {
  padding-left: 30px;
  font-size: 13px;
}

.cat-dot {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--el-text-color-placeholder);
  flex-shrink: 0;
}

.cat-name {
  flex: 1;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.cat-count {
  font-size: 12px;
  font-variant-numeric: tabular-nums;
  color: var(--el-text-color-secondary);
}

/* 按知识库视图：库卡片 */
.lib-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 14px;
  margin-bottom: 16px;
  min-height: 120px;
}

.lib-empty {
  grid-column: 1 / -1;
}

.lib-card {
  display: flex;
  gap: 12px;
  padding: 16px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 10px;
  background: var(--el-bg-color);
  cursor: pointer;
  transition: all 0.15s;
}

.lib-card:hover {
  border-color: var(--el-color-primary-light-5);
  box-shadow: var(--el-box-shadow-light);
  transform: translateY(-2px);
}

.lib-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  width: 40px;
  height: 40px;
  border-radius: 10px;
  color: var(--el-color-success);
  background: var(--el-color-success-light-9);
}

.lib-meta {
  min-width: 0;
}

.lib-name {
  font-size: 15px;
  font-weight: 600;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.lib-desc {
  height: 38px;
  margin-top: 4px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.lib-stats {
  display: flex;
  gap: 8px;
  margin-top: 6px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.lib-cat {
  padding: 1px 8px;
  border-radius: 9px;
  background: var(--el-fill-color-light);
}

/* 当前库横幅 */
.lib-banner {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 16px;
  margin-bottom: 14px;
  border-radius: 10px;
  background: var(--el-color-success-light-9);
}

.lib-banner-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 34px;
  height: 34px;
  border-radius: 9px;
  color: var(--el-color-success);
  background: var(--el-bg-color);
}

.lib-banner-text {
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.lib-banner-name {
  font-size: 15px;
  font-weight: 600;
}

.lib-banner-desc {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* 分类内库 chips */
.lib-chips {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
}

.chips-label {
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

.lib-chip {
  padding: 4px 14px;
  border: 1px solid var(--el-border-color);
  border-radius: 14px;
  background: var(--el-bg-color);
  font-size: 13px;
  color: var(--el-text-color-regular);
  cursor: pointer;
  transition: all 0.15s;
}

.lib-chip:hover {
  border-color: var(--el-color-primary-light-5);
}

.lib-chip.active {
  border-color: var(--el-color-primary);
  color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
  font-weight: 600;
}

/* 词条列表 */
.pages-panel {
  border-radius: 10px;
}

.pages-panel :deep(.el-card__body) {
  padding: 12px 16px;
}

.pages-list {
  min-height: 260px;
}

.page-row {
  padding: 12px 10px;
  border-bottom: 1px dashed var(--el-border-color-lighter);
  border-radius: 8px;
  cursor: pointer;
}

.page-row:hover {
  background: var(--el-color-primary-light-9);
}

.row-title-line {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.row-title {
  font-size: 14px;
  font-weight: 600;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.row-conf {
  font-size: 12px;
  font-variant-numeric: tabular-nums;
  color: var(--el-color-success);
}

.row-meta {
  display: flex;
  gap: 14px;
  margin-top: 6px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.meta-lib {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  color: var(--el-color-primary);
}

.pagination {
  display: flex;
  justify-content: flex-end;
  margin-top: 10px;
}

/* 抽屉 */
.drawer-body {
  padding: 0;
}

:deep(.el-drawer__body) {
  padding: 0 24px 24px;
}

.drawer-meta {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 14px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.page-body {
  font-size: 14px;
  line-height: 1.75;
  word-break: break-word;
}

.outlinks-box,
.refs-box {
  margin-top: 18px;
}

.refs-title {
  font-size: 13px;
  font-weight: 600;
  margin-bottom: 8px;
}

.outlink-tag {
  margin: 0 6px 6px 0;
}

.ref-item {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin: 4px 0;
  word-break: break-all;
}

/* 分类管理弹窗 */
.cat-add {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 12px;
}

.cat-edit-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 12px;
  padding: 10px;
  border-radius: 8px;
  background: var(--el-fill-color-light);
}

.cat-indent {
  color: var(--el-text-color-placeholder);
}

.cat-tip {
  margin: 10px 0 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--el-text-color-secondary);
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
