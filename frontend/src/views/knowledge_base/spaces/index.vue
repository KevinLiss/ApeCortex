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
              <el-tag size="small" :type="s.visibility === 'public' ? 'success' : s.visibility === 'shared' ? 'warning' : 'info'" effect="plain" round>
                {{ s.visibility === 'public' ? t('knowledge.spaces.publicTag')
                  : s.visibility === 'shared' ? t('knowledge.spaces.sharedTag')
                  : t('knowledge.spaces.privateTag') }}
              </el-tag>
            </div>
          </div>
          <div class="space-desc">{{ s.description || '—' }}</div>
          <div class="space-footer">
            <span class="stat"><el-icon><Document /></el-icon>{{ s.page_count }} {{ t('knowledge.spaces.pages') }}</span>
            <el-button size="small" type="primary" plain round @click.stop="openUpload(s)">
              {{ t('knowledge.spaces.uploadDoc') }}
            </el-button>
            <el-button v-if="s.visibility === 'shared'" size="small" plain round @click.stop="openMembers(s)">
              {{ t('knowledge.spaces.members') }}
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

    <!-- 共享空间（他人共享给我） -->
    <div class="group-title" v-if="sharedSpaces.length">{{ t('knowledge.spaces.sharedGroup') }}</div>
    <el-row :gutter="16" v-if="sharedSpaces.length">
      <el-col v-for="s in sharedSpaces" :key="s.id" :xs="24" :sm="12" :md="8" :lg="6">
        <el-card shadow="hover" class="space-card" @click="openPages(s)">
          <div class="space-head">
            <div class="space-icon"><el-icon :size="20"><Folder /></el-icon></div>
            <div class="space-meta">
              <div class="space-name">{{ s.name }}</div>
              <el-tag size="small" type="warning" effect="plain" round>{{ t('knowledge.spaces.sharedTag') }}</el-tag>
            </div>
          </div>
          <div class="space-desc">{{ s.description || '—' }}</div>
          <div class="space-footer">
            <span class="stat"><el-icon><Document /></el-icon>{{ s.page_count }} {{ t('knowledge.spaces.pages') }}</span>
          </div>
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
            <el-button v-if="s.owned" size="small" type="primary" plain round @click.stop="openUpload(s)">
              {{ t('knowledge.spaces.uploadDoc') }}
            </el-button>
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
        <el-form-item v-if="canCreatePublic" :label="t('knowledge.spaces.visibility')">
          <el-radio-group v-model="newVisibility">
            <el-radio label="private">{{ t('knowledge.spaces.visibilityPrivate') }}</el-radio>
            <el-radio label="shared">{{ t('knowledge.spaces.visibilityShared') }}</el-radio>
            <el-radio label="public">{{ t('knowledge.spaces.visibilityPublic') }}</el-radio>
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

    <!-- 成员管理（共享空间） -->
    <el-dialog v-model="membersVisible" :title="t('knowledge.spaces.membersTitle')" width="520px">
      <div class="member-add">
        <el-input v-model="newMemberId" style="width: 160px" :placeholder="t('knowledge.spaces.memberId')" />
        <el-select v-model="newMemberRole" style="width: 120px">
          <el-option :label="t('knowledge.spaces.roleReader')" value="reader" />
          <el-option :label="t('knowledge.spaces.roleWriter')" value="writer" />
        </el-select>
        <el-button size="small" type="primary" @click="handleAddMember">{{ t('knowledge.spaces.addMember') }}</el-button>
      </div>
      <el-table :data="members" v-loading="membersLoading" size="small">
        <el-table-column prop="name" :label="t('knowledge.spaces.memberName')" min-width="140" />
        <el-table-column :label="t('knowledge.spaces.memberRole')" width="100" align="center">
          <template #default="{ row }">
            <el-tag size="small" :type="row.role === 'writer' ? 'warning' : 'info'" effect="plain" round>
              {{ row.role === 'writer' ? t('knowledge.spaces.roleWriter') : t('knowledge.spaces.roleReader') }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="t('knowledge.spaces.memberActions')" width="90" align="center">
          <template #default="{ row }">
            <el-button link size="small" type="danger" @click="handleRemoveMember(row)">{{ t('common.action.delete') }}</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty :description="t('knowledge.spaces.memberEmpty')" :image-size="60" />
        </template>
      </el-table>
      <p class="member-tip">{{ t('knowledge.spaces.memberTip') }}</p>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Collection, Document, Folder, Plus } from '@element-plus/icons-vue'

import { addKbSpaceMember, createKbSpace, getKbSpaceMembers, getKbSpaces, removeKbSpaceMember, uploadKbDoc } from '@/api'
import { useUserStore } from '@/stores/user'

const { t } = useI18n()
const router = useRouter()
const userStore = useUserStore()

const spaces = ref<any[]>([])
const createVisible = ref(false)
const newName = ref('')
const newDesc = ref('')
const newVisibility = ref('private')
const creating = ref(false)
const uploadVisible = ref(false)
const uploadSpace = ref<any>(null)
const docTitle = ref('')
const docContent = ref('')
const docUrl = ref('')
const uploading = ref(false)
const membersVisible = ref(false)
const membersLoading = ref(false)
const members = ref<any[]>([])
const memberSpace = ref<any>(null)
const newMemberId = ref('')
const newMemberRole = ref('reader')

const ownedSpaces = computed(() => spaces.value.filter((s) => s.owned && s.visibility !== 'public'))
const sharedSpaces = computed(() => spaces.value.filter((s) => !s.owned && s.visibility === 'shared'))
const publicSpaces = computed(() => spaces.value.filter((s) => s.visibility === 'public'))
const canCreatePublic = computed(() => userStore.hasPermission('knowledge_base:promotion:review'))

async function fetchSpaces() {
  try {
    const res: any = await getKbSpaces()
    spaces.value = res?.items ?? []
  } catch (e: any) {
    /* 错误提示由 request 拦截器统一展示（含可读化文案） */
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

async function openMembers(space: any) {
  memberSpace.value = space
  membersVisible.value = true
  newMemberId.value = ''
  newMemberRole.value = 'reader'
  fetchMembers()
}

async function fetchMembers() {
  if (!memberSpace.value) return
  membersLoading.value = true
  try {
    const res: any = await getKbSpaceMembers(memberSpace.value.id)
    members.value = res?.items ?? []
  } catch { /* 拦截器已提示 */ } finally {
    membersLoading.value = false
  }
}

async function handleAddMember() {
  if (!memberSpace.value || !newMemberId.value.trim()) return
  try {
    await addKbSpaceMember(memberSpace.value.id, {
      user_id: Number(newMemberId.value.trim()),
      role: newMemberRole.value,
    })
    ElMessage.success(t('knowledge.spaces.memberSaved'))
    newMemberId.value = ''
    fetchMembers()
  } catch { /* 拦截器已提示 */ }
}

async function handleRemoveMember(row: any) {
  if (!memberSpace.value) return
  try {
    await removeKbSpaceMember(memberSpace.value.id, row.user_id)
    ElMessage.success(t('knowledge.spaces.memberRemoved'))
    fetchMembers()
  } catch { /* 拦截器已提示 */ }
}

async function handleUpload() {
  if (!uploadSpace.value) return
  if (!docTitle.value.trim() || docContent.value.trim().length < 40) {
    ElMessage.warning(t('knowledge.spaces.needDoc'))
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
    /* 错误提示由 request 拦截器统一展示（含可读化文案） */
  } finally {
    uploading.value = false
  }
}

onMounted(fetchSpaces)
</script>

<style scoped>
.member-add {
  display: flex;
  gap: 8px;
  margin-bottom: 12px;
}

.member-tip {
  margin: 12px 0 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--el-text-color-secondary);
}

.visibility-hint {
  margin-top: 6px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--el-text-color-secondary);
}

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
