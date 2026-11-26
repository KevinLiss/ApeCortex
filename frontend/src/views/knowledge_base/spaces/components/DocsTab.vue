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

    <!-- 文档列表 -->
    <el-table :data="docs" v-loading="loading" size="default" class="docs-table">
      <el-table-column :label="t('knowledge.docs.colTitle')" min-width="220">
        <template #default="{ row }">
          <div class="doc-title-cell">
            <el-tag size="small" :type="typeTag(row.source_type)" effect="plain" round class="doc-type">
              {{ typeLabel(row.source_type) }}
            </el-tag>
            <span class="doc-title" @click="openDoc(row)">{{ row.title }}</span>
          </div>
        </template>
      </el-table-column>
      <el-table-column :label="t('knowledge.docs.colChars')" width="90" align="center">
        <template #default="{ row }">{{ row.char_count.toLocaleString() }}</template>
      </el-table-column>
      <el-table-column :label="t('knowledge.docs.colCompiled')" width="110" align="center">
        <template #default="{ row }">
          <span v-if="row.compiled_at" class="compiled-yes">
            <el-icon><CircleCheck /></el-icon>{{ t('knowledge.docs.compiledYes') }}
          </span>
          <span v-else class="compiled-no">{{ t('knowledge.docs.compiledNo') }}</span>
        </template>
      </el-table-column>
      <el-table-column :label="t('knowledge.docs.colPages')" width="100" align="center">
        <template #default="{ row }">
          <el-tag v-if="row.page_count" size="small" type="success" effect="light" round>
            {{ row.page_count }} {{ t('knowledge.docs.pagesUnit') }}
          </el-tag>
          <span v-else class="compiled-no">0</span>
        </template>
      </el-table-column>
      <el-table-column :label="t('common.action.label')" width="200" align="center">
        <template #default="{ row }">
          <el-button link size="small" @click="openDoc(row)">{{ t('knowledge.docs.view') }}</el-button>
          <el-button v-if="canWrite" link size="small" type="primary" @click="handleCompile(row)">
            {{ t('knowledge.docs.recompile') }}
          </el-button>
          <el-button v-if="canWrite && canPush" link size="small" type="warning" @click="openPush(row)">
            {{ t('knowledge.push.pushShort') }}
          </el-button>
          <el-button v-if="canWrite" link size="small" type="danger" @click="handleDelete(row)">
            {{ t('common.action.delete') }}
          </el-button>
        </template>
      </el-table-column>
      <template #empty>
        <el-empty :description="t('knowledge.docs.empty')" :image-size="72">
          <el-button v-if="canWrite" type="primary" :icon="Upload" @click="triggerUpload">
            {{ t('knowledge.docs.uploadFirst') }}
          </el-button>
        </el-empty>
      </template>
    </el-table>

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

    <!-- 推送公开库弹窗 -->
    <el-dialog v-model="pushVisible" :title="t('knowledge.push.pushTitle')" width="460px">
      <el-form label-position="top">
        <el-form-item :label="t('knowledge.push.pushTarget')">
          <el-select v-model="pushTarget" style="width: 100%" clearable>
            <el-option v-for="s in pushTargets" :key="s.id" :value="s.id" :label="s.name" />
          </el-select>
          <div v-if="!pushTargets.length" class="push-empty">{{ t('knowledge.push.noTarget') }}</div>
        </el-form-item>
        <p class="push-tip">{{ t('knowledge.push.pushFlowTip') }}</p>
      </el-form>
      <template #footer>
        <el-button @click="pushVisible = false">{{ t('common.action.cancel') }}</el-button>
        <el-button type="primary" :loading="pushing" :disabled="!pushDoc" @click="handlePush">
          {{ t('knowledge.push.pushSubmit') }}
        </el-button>
      </template>
    </el-dialog>

    <!-- 文档查看抽屉 -->
    <el-drawer v-model="docDrawerVisible" :title="docDetail?.title || ''" size="50%" :z-index="2000">
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
      </div>
    </el-drawer>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import { CircleCheck, Document, Promotion, Upload } from '@element-plus/icons-vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'

import {
  compileKbDoc, deleteKbDoc, getKbPushTargets, getKbSpaceDoc, getKbSpaceDocs, submitKbPublic, uploadKbDoc, uploadKbDocFile,
} from '@/api'
import { useUserStore } from '@/stores/user'

const props = defineProps<{ spaceId: number; canWrite: boolean }>()
const emit = defineEmits<{ (e: 'refresh'): void; (e: 'goto-page', pageId: number): void }>()

const { t } = useI18n()
const userStore = useUserStore()

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
const pushTarget = ref<number | null>(null)
const pushing = ref(false)
const pushTargets = ref<any[]>([])
const canPush = computed(() => userStore.hasPermission('knowledge_base:submission:submit'))

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
  pushTarget.value = null
  try {
    const res: any = await getKbPushTargets()
    pushTargets.value = res?.items ?? []
    pushTarget.value = pushTargets.value[0]?.id ?? null
  } catch { /* 拦截器已提示 */ }
  pushVisible.value = true
}

async function handlePush() {
  if (!pushDoc.value) return
  pushing.value = true
  try {
    await submitKbPublic(props.spaceId, pushDoc.value.id, {
      target_space_id: pushTarget.value ?? undefined,
    })
    ElMessage.success(t('knowledge.push.submitted'))
    pushVisible.value = false
  } catch (e: any) { /* 拦截器已提示（含重复推送幂等文案） */ } finally {
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

.doc-title-cell {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.doc-type {
  flex-shrink: 0;
}

.doc-title {
  font-size: 14px;
  font-weight: 500;
  color: var(--el-text-color-primary);
  cursor: pointer;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.doc-title:hover {
  color: var(--el-color-primary);
}

.compiled-yes {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  color: var(--el-color-success);
}

.compiled-no {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.doc-drawer {
  padding: 0 4px;
}

.doc-meta-row {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 14px;
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
  line-height: 1.75;
  word-break: break-word;
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
