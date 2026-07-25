<template>
  <div class="trigger-management-view">
    <!-- 顶部工具栏 -->
    <div class="page-header">
      <div class="header-left">
        <h2 class="page-title">触发器管理</h2>
        <span class="page-subtitle">cron 定时触发、webhook 入口与知识库定时同步</span>
      </div>
      <div class="header-right">
        <a-select
          v-model:value="filterType"
          placeholder="全部类型"
          allow-clear
          style="width: 140px"
          @change="loadTriggers"
        >
          <a-select-option value="">全部类型</a-select-option>
          <a-select-option value="cron">Cron</a-select-option>
          <a-select-option value="webhook">Webhook</a-select-option>
          <a-select-option value="kb_sync">知识库同步</a-select-option>
        </a-select>
        <a-button type="primary" @click="openCreateModal">
          <template #icon><Plus /></template>
          新建触发器
        </a-button>
      </div>
    </div>

    <!-- 触发器列表 -->
    <a-table
      :columns="columns"
      :data-source="triggers"
      :loading="loading"
      :pagination="false"
      row-key="id"
      class="trigger-table"
    >
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'trigger_type'">
          <a-tag :color="triggerTypeColor(record.trigger_type)">
            {{ record.trigger_type }}
          </a-tag>
        </template>
        <template v-else-if="column.key === 'is_active'">
          <a-tag :color="record.is_active ? 'green' : 'default'">
            {{ record.is_active ? '启用' : '禁用' }}
          </a-tag>
        </template>
        <template v-else-if="column.key === 'last_run_status'">
          <a-tag v-if="record.last_run_status" :color="runStatusColor(record.last_run_status)">
            {{ record.last_run_status }}
          </a-tag>
          <span v-else class="text-secondary">—</span>
        </template>
        <template v-else-if="column.key === 'last_run_at'">
          <span class="text-secondary">{{ formatTime(record.last_run_at) }}</span>
        </template>
        <template v-else-if="column.key === 'run_count'">
          <span>{{ record.run_count || 0 }}</span>
        </template>
        <template v-else-if="column.key === 'actions'">
          <a-space :size="4">
            <!-- kb_sync 无 AgentRun，隐藏执行历史入口 -->
            <a-button
              v-if="record.trigger_type !== 'kb_sync'"
              type="link"
              size="small"
              @click="openRunsDrawer(record)"
            >历史</a-button>
            <a-button type="link" size="small" @click="openEditModal(record)">编辑</a-button>
            <a-button
              v-if="record.trigger_type === 'webhook'"
              type="link"
              size="small"
              @click="copyWebhookUrl(record)"
            >复制 URL</a-button>
            <a-button
              v-if="record.trigger_type === 'webhook'"
              type="link"
              size="small"
              @click="confirmRotateSecret(record)"
            >轮换密钥</a-button>
            <a-popconfirm title="确认删除？" @confirm="confirmDelete(record)">
              <a-button type="link" size="small" danger>删除</a-button>
            </a-popconfirm>
          </a-space>
        </template>
      </template>
    </a-table>

    <!-- 创建/编辑弹窗 -->
    <a-modal
      v-model:open="formModalOpen"
      :title="editingTrigger ? '编辑触发器' : '新建触发器'"
      :confirm-loading="submitting"
      width="640px"
      @ok="submitForm"
    >
      <a-form layout="vertical" :model="formData">
        <a-form-item label="名称" required>
          <a-input v-model:value="formData.name" placeholder="如：每日早报" />
        </a-form-item>
        <a-form-item label="描述">
          <a-input v-model:value="formData.desc" placeholder="可选" />
        </a-form-item>
        <a-form-item v-if="formData.trigger_type !== 'kb_sync'" label="关联智能体" required>
          <a-input v-model:value="formData.agent_id" placeholder="Agent slug，如 ChatbotAgent" :disabled="!!editingTrigger" />
        </a-form-item>
        <a-form-item label="触发器类型" required>
          <a-radio-group v-model:value="formData.trigger_type" :disabled="!!editingTrigger">
            <a-radio value="cron">Cron 定时</a-radio>
            <a-radio value="webhook">Webhook 入口</a-radio>
            <a-radio value="kb_sync">知识库同步</a-radio>
          </a-radio-group>
        </a-form-item>

        <!-- kb_sync 配置：目标知识库 -->
        <a-form-item v-if="formData.trigger_type === 'kb_sync'" label="目标知识库" required>
          <a-select
            v-model:value="kbSyncKbId"
            :loading="kbListLoading"
            placeholder="选择要定时同步的知识库"
            show-search
            option-filter-prop="label"
          >
            <a-select-option v-for="kb in kbList" :key="kb.kb_id" :value="kb.kb_id" :label="kb.name">
              {{ kb.name }}
            </a-select-option>
          </a-select>
          <div class="form-tip">到点后重新抓取库内 URL 来源文档，内容变化才重建索引</div>
        </a-form-item>

        <!-- cron / kb_sync 共用定时配置 -->
        <template v-if="formData.trigger_type === 'cron' || formData.trigger_type === 'kb_sync'">
          <a-form-item label="Cron 表达式" required>
            <a-input v-model:value="cronConfig.cron_expr" placeholder="如：0 8 * * *（每天 8:00）" />
            <div class="form-tip">分钟 小时 日 月 周，支持 5 段标准 cron</div>
          </a-form-item>
          <a-form-item label="时区">
            <a-input v-model:value="cronConfig.timezone" placeholder="Asia/Shanghai" />
          </a-form-item>
        </template>

        <!-- webhook 配置 -->
        <template v-if="formData.trigger_type === 'webhook'">
          <a-form-item label="Webhook Secret">
            <a-input
              :value="webhookSecretMasked"
              placeholder="保存后自动生成"
              readonly
              addon-after="自动生成"
            />
            <div class="form-tip">保存后可在「轮换密钥」中重新生成</div>
          </a-form-item>
        </template>

        <a-form-item v-if="formData.trigger_type !== 'kb_sync'" label="触发时的输入 Query">
          <a-textarea
            v-model:value="formData.input_query"
            :rows="2"
            placeholder="留空使用默认 query"
          />
        </a-form-item>
        <a-form-item label="启用状态">
          <a-switch v-model:checked="formData.is_active" />
        </a-form-item>
      </a-form>
    </a-modal>

    <!-- 执行历史抽屉 -->
    <a-drawer
      v-model:open="runsDrawerOpen"
      :title="`执行历史 - ${runsDrawerTrigger?.name || ''}`"
      width="640"
      placement="right"
    >
      <a-table
        :columns="runColumns"
        :data-source="runs"
        :loading="runsLoading"
        :pagination="false"
        row-key="id"
        size="small"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'status'">
            <a-tag :color="runStatusColor(record.status)">{{ record.status }}</a-tag>
          </template>
          <template v-else-if="column.key === 'created_at'">
            {{ formatTime(record.created_at) }}
          </template>
          <template v-else-if="column.key === 'agent_id'">
            <span class="text-secondary">{{ record.agent_id }}</span>
          </template>
        </template>
      </a-table>
    </a-drawer>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { message, Modal } from 'ant-design-vue'
import { Plus } from 'lucide-vue-next'
import { triggerApi } from '@/apis/trigger'
import { databaseApi } from '@/apis/knowledge_api'

// ---------------------------------------------------------------------------
// 列表
// ---------------------------------------------------------------------------
const triggers = ref([])
const loading = ref(false)
const filterType = ref('')

const columns = [
  { title: '名称', dataIndex: 'name', key: 'name' },
  { title: '类型', dataIndex: 'trigger_type', key: 'trigger_type', width: 100 },
  { title: 'Agent', dataIndex: 'agent_id', key: 'agent_id', width: 160 },
  { title: '状态', dataIndex: 'is_active', key: 'is_active', width: 90 },
  { title: '上次状态', dataIndex: 'last_run_status', key: 'last_run_status', width: 110 },
  { title: '上次执行', dataIndex: 'last_run_at', key: 'last_run_at', width: 180 },
  { title: '执行次数', dataIndex: 'run_count', key: 'run_count', width: 90 },
  { title: '操作', key: 'actions', width: 280, fixed: 'right' },
]

const runColumns = [
  { title: 'Run ID', dataIndex: 'id', key: 'id', ellipsis: true },
  { title: '状态', dataIndex: 'status', key: 'status', width: 100 },
  { title: 'Agent', dataIndex: 'agent_id', key: 'agent_id', width: 140 },
  { title: '创建时间', dataIndex: 'created_at', key: 'created_at', width: 180 },
]

async function loadTriggers() {
  loading.value = true
  try {
    const data = await triggerApi.list({ trigger_type: filterType.value || undefined })
    triggers.value = data.triggers || []
  } catch (err) {
    message.error(err.message || '加载触发器失败')
  } finally {
    loading.value = false
  }
}

// ---------------------------------------------------------------------------
// 创建 / 编辑
// ---------------------------------------------------------------------------
const formModalOpen = ref(false)
const editingTrigger = ref(null)
const submitting = ref(false)

const formData = reactive({
  name: '',
  desc: '',
  agent_id: '',
  trigger_type: 'cron',
  input_query: '',
  is_active: true,
})

const cronConfig = reactive({
  cron_expr: '',
  timezone: 'Asia/Shanghai',
})

// webhook secret 仅在编辑模式下展示（masked）
const webhookSecret = ref('')
const webhookSecretMasked = computed(() => {
  if (!webhookSecret.value) return ''
  if (webhookSecret.value.length <= 8) return '***'
  return webhookSecret.value.slice(0, 8) + '***'
})

// kb_sync：目标知识库下拉（懒加载，只拉一次）
const kbSyncKbId = ref(undefined)
const kbList = ref([])
const kbListLoading = ref(false)

async function loadKbList() {
  if (kbList.value.length) return
  kbListLoading.value = true
  try {
    const data = await databaseApi.getAccessibleDatabases()
    kbList.value = data.databases || []
  } catch (err) {
    message.error(err.message || '加载知识库列表失败')
  } finally {
    kbListLoading.value = false
  }
}

function openCreateModal() {
  editingTrigger.value = null
  Object.assign(formData, {
    name: '', desc: '', agent_id: '', trigger_type: 'cron',
    input_query: '', is_active: true,
  })
  Object.assign(cronConfig, { cron_expr: '', timezone: 'Asia/Shanghai' })
  webhookSecret.value = ''
  kbSyncKbId.value = undefined
  loadKbList()
  formModalOpen.value = true
}

function openEditModal(record) {
  editingTrigger.value = record
  Object.assign(formData, {
    name: record.name,
    desc: record.desc || '',
    agent_id: record.agent_id,
    trigger_type: record.trigger_type,
    input_query: record.input_query || '',
    is_active: record.is_active,
  })
  if (record.trigger_type === 'cron') {
    Object.assign(cronConfig, {
      cron_expr: record.config?.cron_expr || '',
      timezone: record.config?.timezone || 'Asia/Shanghai',
    })
  } else if (record.trigger_type === 'webhook') {
    webhookSecret.value = record.config?.secret || ''
  } else if (record.trigger_type === 'kb_sync') {
    Object.assign(cronConfig, {
      cron_expr: record.config?.cron_expr || '',
      timezone: record.config?.timezone || 'Asia/Shanghai',
    })
    kbSyncKbId.value = record.config?.kb_id || undefined
    loadKbList()
  }
  formModalOpen.value = true
}

async function submitForm() {
  const isKbSync = formData.trigger_type === 'kb_sync'
  if (!formData.name || (!isKbSync && !formData.agent_id)) {
    message.warning(isKbSync ? '名称不能为空' : '名称和 Agent 不能为空')
    return
  }
  if ((formData.trigger_type === 'cron' || isKbSync) && !cronConfig.cron_expr) {
    message.warning('Cron 表达式不能为空')
    return
  }
  if (isKbSync && !kbSyncKbId.value) {
    message.warning('请选择目标知识库')
    return
  }

  submitting.value = true
  try {
    if (editingTrigger.value) {
      // 更新
      const fields = {
        name: formData.name,
        desc: formData.desc,
        input_query: formData.input_query || null,
        is_active: formData.is_active,
      }
      if (formData.trigger_type === 'cron') {
        fields.config = {
          cron_expr: cronConfig.cron_expr,
          timezone: cronConfig.timezone || 'Asia/Shanghai',
        }
      } else if (isKbSync) {
        fields.config = {
          cron_expr: cronConfig.cron_expr,
          timezone: cronConfig.timezone || 'Asia/Shanghai',
          kb_id: kbSyncKbId.value,
        }
      }
      await triggerApi.update(editingTrigger.value.id, fields)
      message.success('已更新')
    } else {
      // 创建
      const cronFields = {
        cron_expr: cronConfig.cron_expr,
        timezone: cronConfig.timezone || 'Asia/Shanghai',
      }
      const payload = {
        name: formData.name,
        desc: formData.desc || '',
        trigger_type: formData.trigger_type,
        agent_id: isKbSync ? null : formData.agent_id,
        input_query: isKbSync ? null : formData.input_query || null,
        is_active: formData.is_active,
        config:
          formData.trigger_type === 'cron'
            ? cronFields
            : isKbSync
              ? { ...cronFields, kb_id: kbSyncKbId.value }
              : {},
      }
      const data = await triggerApi.create(payload)
      // 创建 webhook 触发器后展示完整 secret
      if (formData.trigger_type === 'webhook' && data.trigger?.config?.secret) {
        Modal.success({
          title: 'Webhook 触发器已创建',
          content: `请妥善保存以下 secret（仅此一次可见）：\n${data.trigger.config.secret}`,
          width: 600,
        })
      } else {
        message.success('已创建')
      }
    }
    formModalOpen.value = false
    await loadTriggers()
  } catch (err) {
    message.error(err.message || '保存失败')
  } finally {
    submitting.value = false
  }
}

// ---------------------------------------------------------------------------
// 删除 / 轮换 secret / 复制 URL
// ---------------------------------------------------------------------------
async function confirmDelete(record) {
  try {
    await triggerApi.remove(record.id)
    message.success('已删除')
    await loadTriggers()
  } catch (err) {
    message.error(err.message || '删除失败')
  }
}

function confirmRotateSecret(record) {
  Modal.confirm({
    title: '轮换 Webhook Secret',
    content: '旧 secret 将立即失效，确定继续？',
    onOk: async () => {
      try {
        const data = await triggerApi.rotateSecret(record.id)
        Modal.success({
          title: '新 Secret 已生成',
          content: `请妥善保存：\n${data.trigger.config.secret}`,
          width: 600,
        })
        await loadTriggers()
      } catch (err) {
        message.error(err.message || '轮换失败')
      }
    },
  })
}

async function copyWebhookUrl(record) {
  const url = `${window.location.origin}/api/triggers/${record.id}/invoke`
  try {
    await navigator.clipboard.writeText(url)
    message.success('Webhook URL 已复制')
  } catch {
    message.info(url)
  }
}

// ---------------------------------------------------------------------------
// 执行历史抽屉
// ---------------------------------------------------------------------------
const runsDrawerOpen = ref(false)
const runsDrawerTrigger = ref(null)
const runs = ref([])
const runsLoading = ref(false)

async function openRunsDrawer(record) {
  runsDrawerTrigger.value = record
  runsDrawerOpen.value = true
  runsLoading.value = true
  try {
    const data = await triggerApi.runs(record.id, { limit: 50 })
    runs.value = data.runs || []
  } catch (err) {
    message.error(err.message || '加载历史失败')
  } finally {
    runsLoading.value = false
  }
}

// ---------------------------------------------------------------------------
// 工具函数
// ---------------------------------------------------------------------------
function triggerTypeColor(type) {
  const map = { cron: 'blue', webhook: 'purple', kb_sync: 'cyan' }
  return map[type] || 'default'
}

function runStatusColor(status) {
  const map = {
    completed: 'green',
    failed: 'red',
    cancelled: 'default',
    interrupted: 'orange',
    running: 'blue',
    pending: 'default',
    cancel_requested: 'orange',
  }
  return map[status] || 'default'
}

function formatTime(iso) {
  if (!iso) return '—'
  try {
    const d = new Date(iso)
    if (Number.isNaN(d.getTime())) return iso
    return d.toLocaleString('zh-CN', { hour12: false })
  } catch {
    return iso
  }
}

onMounted(() => {
  loadTriggers()
})
</script>

<style lang="less" scoped>
.trigger-management-view {
  padding: 24px;
  background: var(--color-bg-container);
  min-height: 100%;
}

.page-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 20px;

  .header-left {
    display: flex;
    align-items: baseline;
    gap: 12px;

    .page-title {
      margin: 0;
      font-size: 20px;
      font-weight: 600;
      color: var(--color-text);
    }

    .page-subtitle {
      color: var(--color-text-tertiary);
      font-size: 13px;
    }
  }

  .header-right {
    display: flex;
    align-items: center;
    gap: 12px;
  }
}

.trigger-table {
  background: var(--color-bg-container);
  border-radius: 8px;
}

.text-secondary {
  color: var(--color-text-secondary);
}

.form-tip {
  margin-top: 4px;
  color: var(--color-text-tertiary);
  font-size: 12px;
}
</style>
