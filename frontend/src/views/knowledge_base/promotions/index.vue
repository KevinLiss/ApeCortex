<template>
  <div class="kb-push-page">
    <div class="page-header">
      <div>
        <h2>{{ t('knowledge.promotions.title') }}</h2>
        <p class="text-muted">{{ t('knowledge.promotions.subtitle') }}</p>
      </div>
    </div>

    <el-tabs v-model="tab">
      <el-tab-pane v-if="canReview" :label="t('knowledge.push.tabPush')" name="records">
        <PushRecordsTab v-if="tab === 'records'" />
      </el-tab-pane>
      <el-tab-pane v-if="canDispose" :label="t('knowledge.violation.tabViolation')" name="violation">
        <ViolationTab v-if="tab === 'violation'" />
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useUserStore } from '@/stores/user'
import PushRecordsTab from './components/PushRecordsTab.vue'
import ViolationTab from './components/ViolationTab.vue'

const { t } = useI18n()
const userStore = useUserStore()
const tab = ref('records')

const canReview = computed(() => userStore.hasPermission('knowledge_base:submission:review'))
const canDispose = computed(() => userStore.hasPermission('knowledge_base:submission:dispose'))
</script>

<style scoped>
.page-header {
  margin-bottom: 16px;
}

.page-header h2 {
  margin: 0 0 4px;
  font-size: 20px;
}
</style>
