<template>
  <div class="kb-spaces-page">
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
        <el-card shadow="hover" class="space-card" @click="openPages(s)">
          <div class="space-head">
            <div class="space-icon"><el-icon :size="20"><Folder /></el-icon></div>
            <div class="space-meta">
              <div class="space-name">{{ s.name }}</div>
              <el-tag size="small" type="info" effect="plain" round>{{ t('knowledge.spaces.privateTag') }}</el-tag>
            </div>
          </div>
          <div class="space-desc">{{ s.description || '—' }}</div>
          <div class="space-footer">
            <span class="stat"><el-icon><Document /></el-icon>{{ s.page_count }} {{ t('knowledge.spaces.pages') }}</span>
            <el-button size="small" type="primary" plain round @click.stop="openUpload(s)">
              {{ t('knowledge.spaces.uploadDoc') }}
            </el-button>
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

    <!-- 公开空间 -->
    <div class="group-title" v-if="publicSpaces.length">{{ t('knowledge.spaces.publicGroup') }}</div>
    <el-row :gutter="16" v-if="publicSpaces.length">
      <el-col v-for="s in publicSpaces" :key="s.id" :xs="24" :sm="12" :md="8" :lg="6">
        <el-card shadow="hover" class="space-card public" @click="openPages(s)">
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
          </div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 新建空间 -->
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

    <!-- 上传文档 -->
    <el-dialog v-model="uploadVisible" :title="t('knowledge.spaces.uploadTitle')" width="560px">
      <el-form label-position="top">
        <el-form-item :label="t('knowledge.spaces.docTitle')">
          <el-input v-model="docTitle" maxlength="200" />
        </el-form-item>
        <el-form-item :label="t('knowledge.spaces.docContent')">
          <el-input v-model="docContent" type="textarea" :rows="10" />
        </el-form-item>
        <el-form-item :label="t('knowledge.spaces.docUrl')">
          <el-input v-model="docUrl" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="uploadVisible = false">{{ t('common.action.cancel') }}</el-button>
        <el-button type="primary" :loading="uploading" @click="handleUpload">
          {{ uploading ? t('knowledge.spaces.uploading') : t('common.action.confirm') }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Collection, Document, Folder, Plus } from '@element-plus/icons-vue'

import { createKbSpace, getKbSpaces, uploadKbDoc } from '@/api'

const { t } = useI18n()
const router = useRouter()

const spaces = ref<any[]>([])
const createVisible = ref(false)
const newName = ref('')
const newDesc = ref('')
const creating = ref(false)
const uploadVisible = ref(false)
const uploadSpace = ref<any>(null)
const docTitle = ref('')
const docContent = ref('')
const docUrl = ref('')
const uploading = ref(false)

const ownedSpaces = computed(() => spaces.value.filter((s) => s.owned))
const publicSpaces = computed(() => spaces.value.filter((s) => !s.owned && s.visibility === 'public'))

async function fetchSpaces() {
  try {
    const res: any = await getKbSpaces()
    spaces.value = res?.items ?? []
  } catch (e: any) {
    ElMessage.error(e?.message || 'load failed')
  }
}

function openPages(space: any) {
  router.push({ path: '/knowledge-base/pages', query: { space_id: space.id } })
}

function openUpload(space: any) {
  uploadSpace.value = space
  docTitle.value = ''
  docContent.value = ''
  docUrl.value = ''
  uploadVisible.value = true
}

async function handleCreate() {
  if (!newName.value.trim()) return
  creating.value = true
  try {
    await createKbSpace({ name: newName.value.trim(), description: newDesc.value.trim() })
    ElMessage.success('OK')
    createVisible.value = false
    newName.value = ''
    newDesc.value = ''
    fetchSpaces()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.msg || e?.message || 'Error')
  } finally {
    creating.value = false
  }
}

async function handleUpload() {
  if (!uploadSpace.value) return
  if (!docTitle.value.trim() || docContent.value.trim().length < 40) {
    ElMessage.warning(t('knowledge.spaces.noSpaceSelected'))
    return
  }
  uploading.value = true
  try {
    await uploadKbDoc(uploadSpace.value.id, {
      title: docTitle.value.trim(),
      content: docContent.value,
      url: docUrl.value.trim() || undefined,
      source_type: 'upload',
    })
    ElMessage.success(t('knowledge.spaces.uploaded'))
    uploadVisible.value = false
    fetchSpaces()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.msg || e?.message || 'Error')
  } finally {
    uploading.value = false
  }
}

onMounted(fetchSpaces)
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
</style>
