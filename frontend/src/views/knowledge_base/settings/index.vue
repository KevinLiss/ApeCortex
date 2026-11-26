<template>
  <div class="kb-settings-page">
    <div class="page-header">
      <div>
        <h2>{{ t('knowledge.settings.title') }}</h2>
        <p class="text-muted">{{ t('knowledge.settings.subtitle') }}</p>
      </div>
    </div>

    <el-row :gutter="16">
      <el-col :xs="24" :md="12">
        <el-card shadow="never" class="panel">
          <template #header>
            <div class="panel-header">
              <el-icon class="panel-icon"><Connection /></el-icon>
              <span>{{ t('knowledge.settings.webTitle') }}</span>
            </div>
          </template>
          <p class="card-desc">{{ t('knowledge.settings.webDesc') }}</p>
          <div class="switch-line">
            <span>{{ t('knowledge.settings.webEnable') }}</span>
            <el-switch v-model="webEnabled" @change="onSwitch" />
          </div>
          <div class="dom-box">
            <div class="dom-title">{{ t('knowledge.settings.domainAllow') }}</div>
            <div v-for="(d, i) in domainAllow" :key="i" class="dom-row">
              <el-tag closable effect="plain" round @close="domainAllow.splice(i, 1)">{{ d }}</el-tag>
            </div>
            <div class="dom-add">
              <el-input v-model="newDomain" style="width: 240px" placeholder="example.com" @keyup.enter="addDomain" />
              <el-button size="small" @click="addDomain">{{ t('knowledge.settings.addDomain') }}</el-button>
            </div>
          </div>
          <div class="save-line">
            <el-button type="primary" :loading="saving" @click="save" v-permission="'knowledge_base:setting:update'">
              {{ t('knowledge.settings.save') }}
            </el-button>
          </div>
        </el-card>
      </el-col>

      <el-col :xs="24" :md="12">
        <el-card shadow="never" class="panel">
          <template #header>
            <div class="panel-header">
              <el-icon class="panel-icon"><Reading /></el-icon>
              <span>{{ t('knowledge.settings.acTitle') }}</span>
            </div>
          </template>
          <p class="card-desc">{{ t('knowledge.settings.acDesc') }}</p>
          <div class="src-list">
            <div class="src-row"><span class="src-name">arXiv</span><el-tag size="small" type="primary" effect="plain" round>always on</el-tag></div>
            <div class="src-row"><span class="src-name">OpenAlex</span><el-tag size="small" type="primary" effect="plain" round>always on</el-tag></div>
            <div class="src-row"><span class="src-name">Semantic Scholar</span><el-tag size="small" type="primary" effect="plain" round>always on</el-tag></div>
          </div>
          <el-alert :title="t('knowledge.settings.privacy')" type="info" :closable="false" show-icon class="privacy" />
        </el-card>
      </el-col>
    </el-row>

    <el-card shadow="never" class="panel keys-panel">
      <template #header>
        <div class="panel-header">
          <el-icon class="panel-icon"><Key /></el-icon>
          <span>{{ t('knowledge.settings.keysTitle') }}</span>
        </div>
      </template>
      <p class="card-desc">{{ t('knowledge.settings.keysDesc') }}</p>
      <div class="dom-add">
        <el-input v-model="newKeyName" style="width: 240px" :placeholder="t('knowledge.settings.keyName')" maxlength="100" />
        <el-button size="small" type="primary" :loading="creatingKey" @click="handleCreateKey" v-permission="'knowledge_base:setting:update'">
          {{ t('knowledge.settings.keyCreate') }}
        </el-button>
      </div>
      <el-table :data="keys" v-loading="keysLoading" size="small" class="keys-table">
        <el-table-column prop="name" :label="t('knowledge.settings.keyName')" min-width="160" />
        <el-table-column prop="request_count" :label="t('knowledge.settings.keyCount')" width="100" align="center" />
        <el-table-column :label="t('knowledge.settings.keyLastUsed')" width="180" align="center">
          <template #default="{ row }">{{ row.last_used_at ? row.last_used_at.replace('T', ' ').slice(0, 19) : '—' }}</template>
        </el-table-column>
        <el-table-column :label="t('knowledge.settings.keyActions')" width="90" align="center">
          <template #default="{ row }">
            <el-button link size="small" type="danger" @click="handleRevokeKey(row)">{{ t('knowledge.settings.keyRevoke') }}</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty :description="t('knowledge.settings.keyEmpty')" :image-size="60" />
        </template>
      </el-table>
      <p class="key-hint">{{ t('knowledge.settings.keyHint') }}</p>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Connection, Key, Reading } from '@element-plus/icons-vue'

import { createKbPublicKey, deleteKbPublicKey, getKbPublicKeys, getKbWebSettings, putKbWebSettings } from '@/api'

const { t } = useI18n()

const webEnabled = ref(false)
const domainAllow = ref<string[]>([])
const newDomain = ref('')
const saving = ref(false)
const keys = ref<any[]>([])
const keysLoading = ref(false)
const creatingKey = ref(false)
const newKeyName = ref('')
let confirmed = false

async function fetchSettings() {
  try {
    const res: any = await getKbWebSettings()
    webEnabled.value = !!res?.web_enabled
    domainAllow.value = res?.domain_allow ?? []
    confirmed = webEnabled.value
  } catch (e: any) {
    /* 错误提示由 request 拦截器统一展示（含可读化文案） */
  }
}

async function onSwitch(val: boolean) {
  if (val && !confirmed) {
    try {
      await ElMessageBox.confirm(t('knowledge.settings.webConfirm'), '', { type: 'warning' })
      confirmed = true
    } catch {
      webEnabled.value = false
      return
    }
  }
}

function addDomain() {
  const d = newDomain.value.trim().toLowerCase()
  if (!d || domainAllow.value.includes(d)) return
  domainAllow.value.push(d)
  newDomain.value = ''
}

async function save() {
  saving.value = true
  try {
    await putKbWebSettings({
      web_enabled: webEnabled.value,
      domain_allow: domainAllow.value,
      confirm: true,
    })
    ElMessage.success(t('knowledge.settings.saved'))
  } catch (e: any) {
    /* 错误提示由 request 拦截器统一展示（含可读化文案） */
  } finally {
    saving.value = false
  }
}

async function fetchKeys() {
  keysLoading.value = true
  try {
    const res: any = await getKbPublicKeys()
    keys.value = res?.items ?? []
  } catch { /* 拦截器已提示 */ } finally {
    keysLoading.value = false
  }
}

async function handleCreateKey() {
  if (!newKeyName.value.trim()) return
  creatingKey.value = true
  try {
    const res: any = await createKbPublicKey({ name: newKeyName.value.trim() })
    newKeyName.value = ''
    await fetchKeys()
    if (res?.key) {
      await ElMessageBox.alert(res.key, t('knowledge.settings.keyCreated'), {
        confirmButtonText: t('common.action.confirm'),
        customClass: 'key-reveal-box',
      })
    }
  } catch { /* 拦截器已提示 */ } finally {
    creatingKey.value = false
  }
}

async function handleRevokeKey(row: any) {
  try {
    await ElMessageBox.confirm(`${t('knowledge.settings.keyRevoke')} ${row.name}?`, '', { type: 'warning' })
  } catch { return }
  try {
    await deleteKbPublicKey(row.id)
    ElMessage.success(t('knowledge.settings.keyRevoked'))
    fetchKeys()
  } catch { /* 拦截器已提示 */ }
}

onMounted(() => {
  fetchSettings()
  fetchKeys()
})
</script>

<style scoped>
.keys-panel {
  margin-top: 16px;
}

.keys-table {
  margin-top: 12px;
}

.key-hint {
  margin: 12px 0 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--el-text-color-secondary);
}

.page-header {
  margin-bottom: 20px;
}

.page-header h2 {
  margin: 0 0 4px;
  font-size: 20px;
}

.panel {
  border-radius: 10px;
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

.card-desc {
  margin: 4px 0 16px;
  font-size: 13px;
  color: var(--el-text-color-secondary);
  line-height: 1.6;
}

.switch-line {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 12px;
  border-radius: 8px;
  background: var(--el-fill-color-light);
  margin-bottom: 14px;
}

.dom-box {
  margin-top: 8px;
}

.dom-title {
  font-size: 13px;
  margin-bottom: 8px;
  color: var(--el-text-color-secondary);
}

.dom-row {
  display: inline-flex;
  margin: 0 8px 8px 0;
}

.dom-add {
  display: flex;
  gap: 8px;
  margin-top: 8px;
}

.save-line {
  margin-top: 12px;
  display: flex;
  justify-content: flex-end;
}

.src-list {
  margin-top: 8px;
}

.src-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 12px;
  border-radius: 8px;
  background: var(--el-fill-color-light);
  margin-bottom: 8px;
}

.src-name {
  font-size: 13px;
  font-weight: 500;
  font-family: monospace;
}

.privacy {
  margin-top: 14px;
}
</style>
