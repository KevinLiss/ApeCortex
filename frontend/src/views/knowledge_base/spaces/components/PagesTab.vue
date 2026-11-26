<template>
  <div class="pages-tab">
    <!-- 状态筛选 -->
    <div class="type-filter">
      <button v-for="st in statusTabs" :key="st.key" class="type-chip" :class="{ active: status === st.key }" @click="switchStatus(st.key)">
        {{ st.label }}
      </button>
    </div>

    <el-row :gutter="16">
      <!-- 左：词条列表 -->
      <el-col :xs="24" :md="10">
        <el-card shadow="never" class="panel">
          <div v-loading="loading" class="page-list">
            <div v-for="p in pages" :key="p.id" class="page-row" :class="{ active: selected?.id === p.id }" @click="selectPage(p)">
              <div class="row-title-line">
                <span class="row-title">{{ p.title }}</span>
                <el-tag size="small" :type="statusTag(p.status)" effect="light" round>{{ statusLabel(p.status) }}</el-tag>
              </div>
              <div class="row-meta">
                <span class="meta">{{ p.source_count }} {{ t('knowledge.pages.colSources') }}</span>
                <span class="meta">{{ Math.round(p.confidence * 100) }}%</span>
                <span class="meta">v{{ p.version }}</span>
                <span class="meta-links" v-if="(p.out_links || []).length">
                  {{ (p.out_links || []).length }} {{ t('knowledge.pages.outLinks') }}
                </span>
              </div>
            </div>
            <el-empty v-if="!pages.length && !loading" :description="t('knowledge.pages.empty')" :image-size="72" />
          </div>
          <div class="pagination" v-if="total > pageSize">
            <el-pagination v-model:current-page="pageNum" :total="total" :page-size="pageSize"
              layout="prev, pager, next" small background @current-change="fetchPages" />
          </div>
        </el-card>
      </el-col>

      <!-- 右：词条详情 -->
      <el-col :xs="24" :md="14">
        <el-card shadow="never" class="panel" v-if="selected">
          <template #header>
            <div class="panel-header">
              <span class="panel-title">{{ selected.title }}</span>
              <span class="slug-badge">{{ selected.slug }}</span>
              <div class="header-spacer"></div>
              <el-button v-if="canEdit" size="small" @click="startEdit">{{ t('knowledge.pages.edit') }}</el-button>
              <el-button v-if="canPublish && selected.status !== 'live'" size="small" type="success" plain
                @click="handlePublish('live')">{{ t('knowledge.pages.publish') }}</el-button>
              <el-button v-if="canPublish && selected.status === 'live'" size="small" type="warning" plain
                @click="handlePublish('archived')">{{ t('knowledge.pages.unpublish') }}</el-button>
              <el-button v-if="canPromote" size="small" type="primary" plain @click="promoteVisible = true">
                {{ t('knowledge.pages.promote') }}
              </el-button>
            </div>
          </template>

          <div class="page-body" v-html="renderedBody"></div>

          <!-- 溯源（晋升副本） -->
          <div v-if="selected.origin_page_id" class="origin-box">
            <el-icon><Link /></el-icon>
            {{ t('knowledge.pages.fromPromotion') }} space#{{ selected.origin_space_id }}
            · {{ t('knowledge.pages.promotedBy') }} user#{{ selected.origin_author_id }}
          </div>

          <!-- 来源引用 -->
          <div v-if="detailSources.length" class="refs-box">
            <div class="refs-title">{{ t('knowledge.pages.sources') }}</div>
            <div v-for="(r, i) in detailSources" :key="i" class="ref-item">
              {{ i + 1 }}. {{ r.title }}
              <a v-if="r.url" :href="r.url" target="_blank" rel="noopener">{{ r.url }}</a>
            </div>
          </div>

          <el-collapse class="rev-collapse">
            <el-collapse-item :title="t('knowledge.pages.revisions')">
              <div v-for="r in revisions" :key="r.version" class="rev-row">
                <el-tag size="small" :type="r.is_machine_snapshot ? 'info' : 'success'" effect="plain" round>
                  {{ r.is_machine_snapshot ? t('knowledge.pages.revMachine') : t('knowledge.pages.revHuman') }}
                </el-tag>
                <span class="rev-ver">v{{ r.version }}</span>
                <span class="rev-note">{{ r.note }}</span>
              </div>
            </el-collapse-item>
          </el-collapse>
        </el-card>
        <el-card shadow="never" v-else class="panel">
          <el-empty :description="t('knowledge.pages.emptyDesc')" :image-size="80" />
        </el-card>
      </el-col>
    </el-row>

    <!-- 编辑弹窗 -->
    <el-dialog v-model="editVisible" :title="t('knowledge.pages.edit')" width="720px" top="6vh">
      <el-input v-model="editBody" type="textarea" :rows="18" />
      <template #footer>
        <el-button @click="editVisible = false">{{ t('common.action.cancel') }}</el-button>
        <el-button type="primary" :loading="saving" @click="handleSave">{{ t('knowledge.pages.save') }}</el-button>
      </template>
    </el-dialog>

    <!-- 晋升弹窗 -->
    <el-dialog v-model="promoteVisible" :title="t('knowledge.pages.promoteTitle')" width="460px">
      <el-form label-position="top" v-if="selected">
        <el-form-item :label="t('knowledge.pages.promoteTarget')">
          <el-select v-model="promoteTarget" style="width: 100%" :placeholder="t('knowledge.pages.promoteTargetPlaceholder')">
            <el-option v-for="s in publicSpaces" :key="s.id" :value="s.id" :label="s.name" />
          </el-select>
          <div v-if="!publicSpaces.length" class="promote-empty">{{ t('knowledge.pages.promoteNoTarget') }}</div>
        </el-form-item>
        <el-form-item :label="t('knowledge.pages.promoteNote')">
          <el-input v-model="promoteNote" type="textarea" :rows="3" maxlength="500" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="promoteVisible = false">{{ t('common.action.cancel') }}</el-button>
        <el-button type="primary" :disabled="!promoteTarget" @click="handlePromote">{{ t('knowledge.pages.promoteSubmit') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Link } from '@element-plus/icons-vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'

import { getKbPage, getKbPageRevisions, getKbPages, getKbSpaces, publishKbPage, submitKbPromotion, updateKbPage } from '@/api'

const props = defineProps<{ spaceId: number; canEdit: boolean; canPublish: boolean; canPromote: boolean }>()
const emit = defineEmits<{ (e: 'refresh'): void }>()

const { t } = useI18n()

const status = ref('all')
const pages = ref<any[]>([])
const total = ref(0)
const pageNum = ref(1)
const pageSize = 20
const loading = ref(false)
const selected = ref<any>(null)
const detailSources = ref<any[]>([])
const revisions = ref<any[]>([])
const editVisible = ref(false)
const editBody = ref('')
const saving = ref(false)
const promoteVisible = ref(false)
const promoteTarget = ref<number | null>(null)
const promoteNote = ref('')
const publicSpaces = ref<any[]>([])

const statusTabs = computed(() => [
  { key: 'all', label: t('knowledge.pages.statusAll') },
  { key: 'candidate', label: t('knowledge.pages.statusCandidate') },
  { key: 'draft', label: t('knowledge.pages.statusDraft') },
  { key: 'live', label: t('knowledge.pages.statusLive') },
  { key: 'archived', label: t('knowledge.pages.statusArchived') },
])

const renderedBody = computed(() => {
  if (!selected.value?.body_md) return ''
  try {
    return DOMPurify.sanitize(marked.parse(selected.value.body_md) as string)
  } catch {
    return selected.value.body_md
  }
})

function statusTag(s: string): string {
  return { candidate: 'info', draft: 'warning', live: 'success', archived: 'danger' }[s] ?? 'info'
}
function statusLabel(s: string): string {
  const map: Record<string, string> = {
    candidate: t('knowledge.pages.statusCandidate'), draft: t('knowledge.pages.statusDraft'),
    live: t('knowledge.pages.statusLive'), archived: t('knowledge.pages.statusArchived'),
  }
  return map[s] ?? s
}

async function fetchSpaces() {
  const res: any = await getKbSpaces()
  publicSpaces.value = (res?.items ?? []).filter((s: any) => s.visibility === 'public')
}

async function fetchPages() {
  if (!props.spaceId) return
  loading.value = true
  try {
    const params: any = { page: pageNum.value, page_size: pageSize }
    if (status.value !== 'all') params.status = status.value
    const res: any = await getKbPages(props.spaceId, params)
    pages.value = res?.items ?? []
    total.value = res?.total ?? 0
    if (pages.value.length && !selected.value) await selectPage(pages.value[0])
    else if (!pages.value.length) { selected.value = null }
  } finally {
    loading.value = false
  }
}

async function selectPage(p: any) {
  selected.value = p
  const res: any = await getKbPage(p.id)
  selected.value = res
  detailSources.value = res?.source_refs ?? []
  const rev: any = await getKbPageRevisions(p.id, { page: 1, page_size: 10 })
  revisions.value = rev?.items ?? []
}

function switchStatus(key: string) {
  status.value = key
  pageNum.value = 1
  fetchPages()
}

function startEdit() {
  editBody.value = selected.value?.body_md ?? ''
  editVisible.value = true
}

async function handleSave() {
  if (!selected.value) return
  saving.value = true
  try {
    await updateKbPage(selected.value.id, {
      body_md: editBody.value,
      expected_version: selected.value.version,
    })
    ElMessage.success(t('knowledge.pages.saved'))
    editVisible.value = false
    await selectPage(selected.value)
    fetchPages()
  } catch (e: any) {
    if (e?.response?.status === 409) {
      ElMessage.error(t('knowledge.pages.expectedVersionConflict'))
    }
  } finally {
    saving.value = false
  }
}

async function handlePublish(target: string) {
  if (!selected.value) return
  try {
    await ElMessageBox.confirm(
      target === 'live' ? t('knowledge.pages.publishConfirm') : t('knowledge.pages.unpublishConfirm'),
      '', { type: 'warning' }
    )
  } catch { return }
  try {
    await publishKbPage(selected.value.id, target)
    ElMessage.success(t('knowledge.pages.published'))
    await selectPage(selected.value)
    fetchPages()
    emit('refresh')
  } catch (e: any) {
    /* 错误提示由 request 拦截器统一展示（含可读化文案） */
  }
}

async function handlePromote() {
  if (!selected.value || !promoteTarget.value) return
  if (selected.value.source_count < 3 || selected.value.confidence < 0.8) {
    ElMessage.warning(t('knowledge.pages.promoteGateFailDetail', {
      sources: selected.value.source_count ?? 0,
      confidence: Math.round((selected.value.confidence ?? 0) * 100),
    }))
    return
  }
  try {
    await submitKbPromotion({
      page_id: selected.value.id,
      target_space_id: promoteTarget.value,
      submit_note: promoteNote.value,
    })
    ElMessage.success(t('knowledge.pages.promoteSubmitted'))
    promoteVisible.value = false
  } catch (e: any) {
    /* 错误提示由 request 拦截器统一展示（含可读化文案） */
  }
}

function gotoPage(pageId: number) {
  const p = pages.value.find((x: any) => x.id === pageId)
  if (p) selectPage(p)
}

defineExpose({ refresh: fetchPages, gotoPage })

onMounted(() => {
  fetchSpaces()
  fetchPages()
})
</script>

<style scoped>
.promote-empty {
  margin-top: 6px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--el-text-color-secondary);
}

.type-filter {
  display: flex;
  gap: 8px;
  margin-bottom: 14px;
}

.type-chip {
  padding: 5px 14px;
  border: 1px solid var(--el-border-color);
  border-radius: 14px;
  background: var(--el-bg-color);
  color: var(--el-text-color-regular);
  font-size: 13px;
  cursor: pointer;
  transition: all 0.2s;
}

.type-chip.active {
  border-color: var(--el-color-primary);
  color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
  font-weight: 600;
}

.panel {
  border-radius: 10px;
}

.panel :deep(.el-card__body) {
  padding: 12px 16px;
}

.panel-header {
  display: flex;
  align-items: center;
  gap: 8px;
}

.panel-title {
  font-weight: 600;
  font-size: 15px;
}

.slug-badge {
  padding: 2px 8px;
  border-radius: 9px;
  font-size: 11px;
  font-family: monospace;
  color: var(--el-text-color-secondary);
  background: var(--el-fill-color-light);
}

.header-spacer {
  flex: 1;
}

.page-list {
  min-height: 240px;
}

.page-row {
  padding: 10px 8px;
  border-radius: 8px;
  cursor: pointer;
  border-bottom: 1px dashed var(--el-border-color-lighter);
}

.page-row:hover,
.page-row.active {
  background: var(--el-color-primary-light-9);
}

.row-title-line {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.row-title {
  font-size: 14px;
  font-weight: 500;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.row-meta {
  display: flex;
  gap: 12px;
  margin-top: 6px;
  font-size: 12px;
  font-variant-numeric: tabular-nums;
  color: var(--el-text-color-secondary);
}

.meta-links {
  color: var(--el-color-primary);
}

.page-body {
  font-size: 14px;
  line-height: 1.75;
  word-break: break-word;
  min-height: 120px;
}

.origin-box {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 14px;
  padding: 8px 12px;
  border-radius: 8px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  background: var(--el-fill-color-light);
}

.refs-box {
  margin-top: 14px;
}

.refs-title {
  font-size: 13px;
  font-weight: 600;
  margin-bottom: 6px;
}

.ref-item {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin: 3px 0;
}

.rev-collapse {
  margin-top: 10px;
}

.rev-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 0;
  font-size: 12px;
}

.rev-ver {
  font-variant-numeric: tabular-nums;
  color: var(--el-text-color-secondary);
}

.rev-note {
  color: var(--el-text-color-placeholder);
}

.pagination {
  display: flex;
  justify-content: flex-end;
  margin-top: 10px;
}
</style>
