<template>
  <div class="members-tab">
    <div class="member-add">
      <el-input v-model="newMemberId" style="width: 160px" :placeholder="t('knowledge.spaces.memberId')" />
      <el-select v-model="newMemberRole" style="width: 130px">
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
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage } from 'element-plus'

import { addKbSpaceMember, getKbSpaceMembers, removeKbSpaceMember } from '@/api'

const props = defineProps<{ spaceId: number; canManage: boolean }>()

const { t } = useI18n()

const members = ref<any[]>([])
const membersLoading = ref(false)
const newMemberId = ref('')
const newMemberRole = ref('reader')

async function fetchMembers() {
  membersLoading.value = true
  try {
    const res: any = await getKbSpaceMembers(props.spaceId)
    members.value = res?.items ?? []
  } catch { /* 拦截器已提示 */ } finally {
    membersLoading.value = false
  }
}

async function handleAddMember() {
  if (!newMemberId.value.trim()) return
  try {
    await addKbSpaceMember(props.spaceId, {
      user_id: Number(newMemberId.value.trim()),
      role: newMemberRole.value,
    })
    ElMessage.success(t('knowledge.spaces.memberSaved'))
    newMemberId.value = ''
    fetchMembers()
  } catch { /* 拦截器已提示 */ }
}

async function handleRemoveMember(row: any) {
  try {
    await removeKbSpaceMember(props.spaceId, row.user_id)
    ElMessage.success(t('knowledge.spaces.memberRemoved'))
    fetchMembers()
  } catch { /* 拦截器已提示 */ }
}

onMounted(fetchMembers)
defineExpose({ refresh: fetchMembers })
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
</style>
