<template>
  <div class="docs-tab">
    <!-- 工具条 -->
    <div class="docs-toolbar">
      <div class="toolbar-left">
        <el-upload
          :show-file-list="false"
          :http-request="handleFileUpload"
          accept=".md,.markdown,.txt,.pdf,.docx"
          :disabled="!canWrite"
        >
          <el-button type="primary" :icon="Upload" :loading="uploading" :disabled="!canWrite">
            {{ t('knowledge.docs.uploadFile') }}
          </el-button>
        </el-upload>
        <el-button :icon="Document" :disabled="!canWrite" @click="pasteVisible = true">
          {{ t('knowledge.docs.paste') }}
        </el-button>
        <el-button
          v-if="canPush"
          :icon="Promotion"
          :disabled="!canWrite"
          @click="openPush(null)"
        >{{ t('knowledge.push.pushPublic') }}</el-button>
      </div>
      <span class="toolbar-hint">{{ t('knowledge.docs.uploadTip') }}</span>
    </div>

    <!-- 文档列表（紧凑行式，MaxKB 文档列表风格） -->
    <el-card shadow="never" class="docs-list-card">
      <div v-loading="loading" class="docs-list">
        <div v-for="d in docs" :key="d.id" class="doc-row" @click="openDoc(d)">
          <div class="doc-row-main">
            <el-tag size="small" :type="typeTag(d.source_type)" effect="plain" class="doc-type">
              {{ typeLabel(d.source_type) }}
            </el-tag>
            <span class="doc-row-title">{{ d.title }}</span>
            <span v-if="d.compiled_at" class="doc-compiled">
              <el-icon><CircleCheck /></el-icon>{{ t('knowledge.docs.compiledYes') }}
            </span>
            <span v-else class="doc-compiled no">{{ t('knowledge.docs.compiledNo') }}</span>
            <el-tag v-if="d.page_count" size="small" type="success" effect="light" round class="doc-pages">
              {{ d.page_count }} {{ t('knowledge.docs.pagesUnit') }}
            </el-tag>
            <div class="doc-row-actions">
              <el-button link size="small" class="action-download" @click.stop="handleDownload(d)">
                {{ t('knowledge.docs.download') }}
              </el-button>
              <el-button v-if="canWrite" link size="small" type="primary" @click.stop="handleCompile(d)">
                {{ t('knowledge.docs.recompile') }}
              </el-button>
              <el-button v-if="canWrite && canPush" link size="small" type="warning" @click.stop="openPush(d)">
                {{ t('knowledge.push.pushShort') }}
              </el-button>
              <el-button v-if="canWrite" link size="small" type="danger" @click.stop="handleDelete(d)">
                {{ t('common.action.delete') }}
              </el-button>
            </div>
          </div>
          <div class="doc-row-meta">
            <span>{{ d.char_count.toLocaleString() }} {{ t('knowledge.docs.charsUnit') }}</span>
            <span v-if="d.compiled_at">{{ formatTime(d.compiled_at) }}</span>
            <a v-if="d.url" :href="d.url" target="_blank" rel="noopener" class="doc-url" @click.stop>
              {{ d.url }}
            </a>
            <span class="doc-view-hint">{{ t('knowledge.docs.view') }} →</span>
          </div>
        </div>
        <el-empty v-if="!docs.length && !loading" :description="t('knowledge.docs.empty')" :image-size="72">
          <el-button v-if="canWrite" type="primary" :icon="Upload" @click="triggerUpload">
            {{ t('knowledge.docs.uploadFirst') }}
          </el-button>
        </el-empty>
      </div>
      <div v-if="docs.length" class="docs-foot">
        <span class="docs-count">{{ docs.length }} {{ t('knowledge.docs.docUnit') }} ·
          {{ totalChars.toLocaleString() }} {{ t('knowledge.docs.charsUnit') }}</span>
      </div>
    </el-card>

    <!-- 粘贴文本弹窗 -->
    <el-dialog v-model="pasteVisible" :title="t('knowledge.docs.pasteTitle')" width="560px">
      <el-form label-position="top">
        <el-form-item :label="t('knowledge.docs.docTitle')">
          <el-input v-model="pasteTitle" maxlength="200" :placeholder="t('knowledge.spaces.namePlaceholder')" />
        </el-form-item>
        <el-form-item :label="t('knowledge.docs.docContent')">
          <el-input v-model="pasteContent" type="textarea" :rows="10" />
        </el-form-item>
        <el-form-item :label="t('knowledge.docs.docUrl')">
          <el-input v-model="pasteUrl" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="pasteVisible = false">{{ t('common.action.cancel') }}</el-button>
        <el-button type="primary" :loading="uploading" @click="handlePasteUpload">
          {{ t('common.action.confirm') }}
        </el-button>
      </template>
    </el-dialog>

    <!-- 推送知识中心确认弹窗 -->
    <el-dialog v-model="pushVisible" :title="t('knowledge.push.pushTitle')" width="460px">
      <el-alert type="info" :closable="false" show-icon class="push-flow-alert"
        :title="t('knowledge.push.pushFlowTitle')" :description="t('knowledge.push.pushFlowTip')" />
      <p class="push-doc-title" :class="{ hint: !pushDoc }">{{ pushDoc?.title || t('knowledge.push.pushPickHint') }}</p>
      <template #footer>
        <el-button @click="pushVisible = false">{{ t('common.action.cancel') }}</el-button>
        <el-button type="primary" :loading="pushing" :disabled="!pushDoc" @click="handlePush">
          {{ t('knowledge.push.pushSubmit') }}
        </el-button>
      </template>
    </el-dialog>


    <!-- 文档查看抽屉 -->
    <el-drawer v-model="docDrawerVisible" :title="docDetail?.title || ''" size="52%" :z-index="2000" class="kb-doc-drawer">
      <div v-if="docDetail" class="doc-drawer">
        <div class="doc-meta-row">
          <el-tag size="small" :type="typeTag(docDetail.source_type)" effect="plain" round>
            {{ typeLabel(docDetail.source_type) }}
          </el-tag>
          <span>{{ docDetail.char_count.toLocaleString() }} {{ t('knowledge.docs.charsUnit') }}</span>
          <span v-if="docDetail.compiled_at">{{ t('knowledge.docs.compiledYes') }} {{ formatTime(docDetail.compiled_at) }}</span>
        </div>
        <div v-if="docDetail.pages?.length" class="derived-box">
          <div class="derived-title">{{ t('knowledge.docs.derivedPages') }}</div>
          <div
            v-for="p in docDetail.pages"
            :key="p.id"
            class="derived-row"
            @click="$emit('goto-page', p.id)"
          >
            <span class="derived-name">{{ p.title }}</span>
            <el-tag size="small" :type="p.status === 'live' ? 'success' : 'info'" effect="plain" round>
              {{ p.status }}
            </el-tag>
          </div>
        </div>
        <div class="doc-content" v-html="renderedDoc" />
        <div class="drawer-foot" />
      </div>
    </el-drawer>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import { CircleCheck, Document, Download, Promotion, Upload } from '@element-plus/icons-vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'

import {
  compileKbDoc, deleteKbDoc, downloadKbDoc, getKbSpaceDoc, getKbSpaceDocs, submitKbDocPublic, uploadKbDoc, uploadKbDocFile,
} from '@/api'

const props = defineProps<{ spaceId: number; canWrite: boolean }>()
const emit = defineEmits<{ (e: 'refresh'): void; (e: 'goto-page', pageId: number): void }>()

const { t } = useI18n()

const docs = ref<any[]>([])
const loading = ref(false)
const uploading = ref(false)
const pasteVisible = ref(false)
const pasteTitle = ref('')
const pasteContent = ref('')
const pasteUrl = ref('')
const docDrawerVisible = ref(false)
const docDetail = ref<any>(null)

const pushVisible = ref(false)
const pushDoc = ref<any>(null)
const pushing = ref(false)
const canPush = true // 提交人本人（个人空间 owner）即有推送入口；按钮仅受 canWrite 控制
const totalChars = computed(() => docs.value.reduce((acc, d) => acc + (d.char_count || 0), 0))

const renderedDoc = computed(() => {
  if (!docDetail.value?.content) return ''
  try {
    return DOMPurify.sanitize(marked.parse(docDetail.value.content) as string)
  } catch {
    return docDetail.value.content
  }
})

async function fetchDocs() {
  loading.value = true
  try {
    const res: any = await getKbSpaceDocs(props.spaceId)
    docs.value = res?.items ?? []
  } catch { /* 拦截器已提示 */ } finally {
    loading.value = false
  }
}

async function handleFileUpload(options: any) {
  uploading.value = true
  try {
    await uploadKbDocFile(props.spaceId, options.file as File)
    ElMessage.success(t('knowledge.docs.uploaded'))
    await fetchDocs()
    emit('refresh')
  } catch (e: any) { /* 拦截器已提示 */ } finally {
    uploading.value = false
  }
}

function triggerUpload() {
  const el = document.querySelector<HTMLInputElement>('.docs-tab .el-upload')
  el?.querySelector('input')?.click()
}

async function openPush(row: any) {
  pushDoc.value = row
  pushVisible.value = true
}

async function handleDownload(row: any) {
  try {
    const blob: any = await downloadKbDoc(props.spaceId, row.id)
    const url = URL.createObjectURL(new Blob([blob], { type: blob?.type || 'text/markdown' }))
    const a = document.createElement('a')
    const safeTitle = (row.title || 'document').replace(/[/\\ ]+/g, '_').slice(0, 80)
    a.href = url
    a.download = `${safeTitle}.${row.file_ext || 'md'}`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
    ElMessage.success(t('knowledge.docs.downloaded'))
  } catch { /* 拦截器已提示 */ }
}

async function handlePush() {
  if (!pushDoc.value) return
  pushing.value = true
  try {
    await submitKbDocPublic(props.spaceId, pushDoc.value.id)
    ElMessage.success(t('knowledge.push.submitted'))
    pushVisible.value = false
  } catch { /* 拦截器已提示（含重复推送幂等文案） */ } finally {
    pushing.value = false
  }
}

async function handlePasteUpload() {
  if (!pasteTitle.value.trim() || pasteContent.value.trim().length < 40) {
    ElMessage.warning(t('knowledge.docs.needDoc'))
    return
  }
  uploading.value = true
  try {
    await uploadKbDoc(props.spaceId, {
      title: pasteTitle.value.trim(),
      content: pasteContent.value,
      url: pasteUrl.value.trim() || undefined,
      source_type: 'upload',
    })
    ElMessage.success(t('knowledge.docs.uploaded'))
    pasteVisible.value = false
    pasteTitle.value = ''
    pasteContent.value = ''
    pasteUrl.value = ''
    await fetchDocs()
    emit('refresh')
  } catch { /* 拦截器已提示 */ } finally {
    uploading.value = false
  }
}

async function openDoc(row: any) {
  docDrawerVisible.value = true
  try {
    docDetail.value = await getKbSpaceDoc(props.spaceId, row.id)
  } catch { /* 拦截器已提示 */ }
}

async function handleCompile(row: any) {
  try {
    await compileKbDoc(props.spaceId, row.id)
    ElMessage.success(t('knowledge.docs.recompiled'))
  } catch { /* 拦截器已提示（含 429 限额文案） */ }
}

async function handleDelete(row: any) {
  try {
    await ElMessageBox.confirm(t('knowledge.docs.deleteConfirm'), { type: 'warning' })
  } catch { return }
  try {
    await deleteKbDoc(props.spaceId, row.id)
    ElMessage.success(t('knowledge.docs.deleted'))
    await fetchDocs()
    emit('refresh')
  } catch { /* 拦截器已提示 */ }
}

function typeTag(st: string): string {
  return { upload: 'primary', crawl: 'warning', web: 'warning', paper: 'success', research: 'success', chat: 'info' }[st] ?? 'info'
}

function typeLabel(st: string): string {
  const map: Record<string, string> = {
    upload: t('knowledge.docs.typeUpload'),
    crawl: t('knowledge.docs.typeCrawl'),
    web: t('knowledge.docs.typeWeb'),
    paper: t('knowledge.docs.typePaper'),
    research: t('knowledge.docs.typeResearch'),
    chat: t('knowledge.docs.typeChat'),
  }
  return map[st] ?? st
}

function formatTime(iso: string | null): string {
  return (iso || '').slice(0, 16).replace('T', ' ')
}

onMounted(fetchDocs)
defineExpose({ refresh: fetchDocs })
</script>

<style scoped>
.docs-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 10px;
  margin-bottom: 14px;
}

.toolbar-left {
  display: flex;
  gap: 10px;
}

.toolbar-hint {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

/* 文档行式列表（紧凑，与词条列表同设计语言） */
.docs-list-card {
  border-radius: 10px;
}

.docs-list-card :deep(.el-card__body) {
  padding: 6px 8px;
}

.docs-list {
  min-height: 200px;
}

.doc-row {
  padding: 10px 12px;
  border-radius: 8px;
  cursor: pointer;
  transition: background 0.15s;
}

.doc-row:hover {
  background: var(--el-color-primary-light-9);
}

.doc-row-main {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.doc-type {
  flex-shrink: 0;
  border-radius: 5px;
}

.doc-row-title {
  flex: 1;
  min-width: 0;
  font-size: 14px;
  font-weight: 500;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.doc-compiled {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  flex-shrink: 0;
  font-size: 12px;
  color: var(--el-color-success);
}

.doc-compiled.no {
  color: var(--el-text-color-secondary);
}

.doc-pages {
  flex-shrink: 0;
}

.doc-row-actions {
  display: flex;
  gap: 4px;
  flex-shrink: 0;
  opacity: 0;
  transition: opacity 0.15s;
}

.doc-row:hover .doc-row-actions,
.doc-row:focus-within .doc-row-actions {
  opacity: 1;
}

/* 下载是只读操作，常驻可见（不需要写权限，也无需 hover 发现） */
.action-download {
  opacity: 1 !important;
}

/* 无写权限时操作区只有下载，常显整组 */
.doc-row-actions:has(> .action-download:only-child) {
  opacity: 1;
}

.doc-row-meta {
  display: flex;
  align-items: center;
  gap: 14px;
  margin-top: 4px;
  padding-left: 2px;
  font-size: 12px;
  font-variant-numeric: tabular-nums;
  color: var(--el-text-color-secondary);
}

.doc-url {
  max-width: 40%;
  font-size: 12px;
  color: var(--el-color-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.doc-url:hover {
  text-decoration: underline;
}

.doc-view-hint {
  margin-left: auto;
  font-size: 12px;
  color: var(--el-color-primary);
  opacity: 0;
  transition: opacity 0.15s;
}

.doc-row:hover .doc-view-hint {
  opacity: 1;
}

.docs-foot {
  display: flex;
  justify-content: flex-end;
  padding: 8px 12px 4px;
}

.docs-count {
  font-size: 12px;
  font-variant-numeric: tabular-nums;
  color: var(--el-text-color-secondary);
}

/* 抽屉：内容区内边距（覆盖默认 20px，形成 8/16/24 间距节奏） */
:deep(.el-drawer__body) {
  padding: 0 24px 24px;
}

.doc-drawer {
  padding: 0;
}

.drawer-foot {
  height: 8px;
}

.doc-meta-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding-bottom: 12px;
  margin-bottom: 16px;
  border-bottom: 1px solid var(--el-border-color-lighter);
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.derived-box {
  margin-bottom: 18px;
  padding: 12px;
  border-radius: 10px;
  background: var(--el-fill-color-light);
}

.derived-title {
  font-size: 13px;
  font-weight: 600;
  margin-bottom: 8px;
}

.derived-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 6px 10px;
  margin-top: 4px;
  border-radius: 8px;
  background: var(--el-bg-color);
  cursor: pointer;
}

.derived-row:hover {
  color: var(--el-color-primary);
}

.derived-name {
  font-size: 13px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.doc-content {
  font-size: 14px;
  line-height: 1.8;
  word-break: break-word;
}

/* 文档全文排版：长链接断行、引用列表对齐（marker 挂在内容框内，不贴边） */
.doc-content :deep(a) {
  color: var(--el-color-primary);
  text-decoration: none;
  word-break: break-all;
  line-height: 1.6;
}

.doc-content :deep(a:hover) {
  text-decoration: underline;
}

.doc-content :deep(ol),
.doc-content :deep(ul) {
  padding-left: 2em;
  margin: 10px 0;
  list-style-position: outside;
}

.doc-content :deep(li) {
  margin: 4px 0;
}

.doc-content :deep(li::marker) {
  color: var(--el-text-color-secondary);
}

.doc-content :deep(p) {
  margin: 8px 0;
}

.push-flow-alert {
  margin-bottom: 12px;
}

.push-doc-title {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
  word-break: break-all;
}

.push-doc-title.hint {
  font-weight: 400;
  color: var(--el-text-color-secondary);
}

.push-empty {
  margin-top: 6px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.push-tip {
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--el-text-color-secondary);
}
</style>
