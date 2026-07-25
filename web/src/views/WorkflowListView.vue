<template>
  <div class="workflow-list-view">
    <!-- 顶部工具栏 -->
    <div class="page-header">
      <div class="header-left">
        <h2 class="page-title">工作流</h2>
        <span class="page-subtitle">可视化编排多节点执行流程</span>
      </div>
      <div class="header-right">
        <a-button type="primary" @click="openCreateModal">
          <template #icon><Plus /></template>
          新建工作流
        </a-button>
      </div>
    </div>

    <!-- 工作流卡片列表 -->
    <a-spin :spinning="loading">
      <div v-if="workflows.length" class="workflow-grid">
        <div
          v-for="wf in workflows"
          :key="wf.id"
          class="workflow-card"
          @click="goEditor(wf)"
        >
          <div class="card-top">
            <div class="card-icon">
              <Workflow :size="20" />
            </div>
            <div class="card-title-area">
              <div class="card-name">{{ wf.name }}</div>
              <div class="card-slug">{{ wf.slug }}</div>
            </div>
            <a-tag :color="wf.is_active ? 'green' : 'default'" class="card-status">
              {{ wf.is_active ? '启用' : '禁用' }}
            </a-tag>
          </div>
          <div class="card-desc">{{ wf.desc || '暂无描述' }}</div>
          <div class="card-footer">
            <span class="card-meta">
              {{ nodeCount(wf) }} 个节点 · v{{ wf.version }}
            </span>
            <a-space :size="4" @click.stop>
              <a-button type="link" size="small" @click="toggleActive(wf)">
                {{ wf.is_active ? '禁用' : '启用' }}
              </a-button>
              <a-popconfirm title="确认删除该工作流？" @confirm="confirmDelete(wf)">
                <a-button type="link" size="small" danger>删除</a-button>
              </a-popconfirm>
            </a-space>
          </div>
        </div>
      </div>
      <a-empty v-else-if="!loading" description="暂无工作流，点击右上角新建" class="empty-state" />
    </a-spin>

    <!-- 新建弹窗 -->
    <a-modal
      v-model:open="createModalOpen"
      title="新建工作流"
      :confirm-loading="submitting"
      width="520px"
      @ok="submitCreate"
    >
      <a-form layout="vertical" :model="formData">
        <a-form-item label="名称" required>
          <a-input v-model:value="formData.name" placeholder="如：客服问题分流" />
        </a-form-item>
        <a-form-item label="Slug" required>
          <a-input v-model:value="formData.slug" placeholder="唯一标识，如 support-routing" />
          <div class="form-tip">全局唯一，创建后不可修改</div>
        </a-form-item>
        <a-form-item label="描述">
          <a-textarea v-model:value="formData.desc" :rows="2" placeholder="可选" />
        </a-form-item>
      </a-form>
    </a-modal>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import { Plus, Workflow } from 'lucide-vue-next'
import { workflowApi } from '@/apis/workflow_api'

const router = useRouter()

// ---------------------------------------------------------------------------
// 列表
// ---------------------------------------------------------------------------
const workflows = ref([])
const loading = ref(false)

async function loadWorkflows() {
  loading.value = true
  try {
    const data = await workflowApi.list()
    workflows.value = data.workflows || []
  } catch (err) {
    message.error(err.message || '加载工作流失败')
  } finally {
    loading.value = false
  }
}

function nodeCount(wf) {
  return wf.definition?.nodes?.length || 0
}

function goEditor(wf) {
  router.push(`/workflows/${wf.id}`)
}

async function toggleActive(wf) {
  try {
    await workflowApi.update(wf.id, { is_active: !wf.is_active })
    wf.is_active = !wf.is_active
    message.success(wf.is_active ? '已启用' : '已禁用')
  } catch (err) {
    message.error(err.message || '操作失败')
  }
}

async function confirmDelete(wf) {
  try {
    await workflowApi.remove(wf.id)
    message.success('已删除')
    loadWorkflows()
  } catch (err) {
    message.error(err.message || '删除失败')
  }
}

// ---------------------------------------------------------------------------
// 新建
// ---------------------------------------------------------------------------
const createModalOpen = ref(false)
const submitting = ref(false)

const formData = reactive({
  name: '',
  slug: '',
  desc: '',
})

function openCreateModal() {
  Object.assign(formData, { name: '', slug: '', desc: '' })
  createModalOpen.value = true
}

async function submitCreate() {
  if (!formData.name || !formData.slug) {
    message.warning('名称和 Slug 不能为空')
    return
  }
  submitting.value = true
  try {
    const wf = await workflowApi.create({
      name: formData.name,
      slug: formData.slug,
      desc: formData.desc,
    })
    createModalOpen.value = false
    message.success('创建成功')
    router.push(`/workflows/${wf.id}`)
  } catch (err) {
    message.error(err.message || '创建失败')
  } finally {
    submitting.value = false
  }
}

onMounted(loadWorkflows)
</script>

<style lang="less" scoped>
.workflow-list-view {
  padding: 24px;
  background: var(--color-bg-container);
  min-height: 100%;
}

.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 20px;

  .header-left {
    display: flex;
    align-items: baseline;
    gap: 12px;
  }

  .page-title {
    font-size: 20px;
    font-weight: 600;
    color: var(--color-text);
    margin: 0;
  }

  .page-subtitle {
    font-size: 13px;
    color: var(--color-text-secondary);
  }
}

.workflow-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 16px;
}

.workflow-card {
  border: 1px solid var(--gray-150);
  border-radius: 8px;
  padding: 16px;
  cursor: pointer;
  background: var(--gray-0);
  transition: border-color 0.2s, background 0.2s;

  &:hover {
    border-color: var(--main-color);
  }

  .card-top {
    display: flex;
    align-items: center;
    gap: 10px;
  }

  .card-icon {
    width: 36px;
    height: 36px;
    border-radius: 8px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: var(--main-30);
    color: var(--main-color);
    flex-shrink: 0;
  }

  .card-title-area {
    flex: 1;
    min-width: 0;
  }

  .card-name {
    font-size: 15px;
    font-weight: 600;
    color: var(--color-text);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .card-slug {
    font-size: 12px;
    color: var(--color-text-tertiary);
    font-family: monospace;
  }

  .card-status {
    flex-shrink: 0;
    margin-right: 0;
  }

  .card-desc {
    margin: 12px 0;
    font-size: 13px;
    color: var(--color-text-secondary);
    min-height: 20px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .card-footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    border-top: 1px solid var(--gray-150);
    padding-top: 10px;

    .card-meta {
      font-size: 12px;
      color: var(--color-text-tertiary);
    }
  }
}

.empty-state {
  margin-top: 80px;
}

.form-tip {
  font-size: 12px;
  color: var(--color-text-tertiary);
  margin-top: 4px;
}
</style>
