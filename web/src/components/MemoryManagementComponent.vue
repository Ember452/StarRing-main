<template>
  <div class="memory-management">
    <div class="header-section">
      <div class="header-content">
        <div class="section-title">记忆管理</div>
        <p class="section-description">
          智能体在对话中记住的关于你的长期事实（需在智能体配置中开启「长期记忆」）。记忆仅自己可见。
        </p>
      </div>
      <div class="header-actions">
        <a-button class="lucide-icon-btn" :loading="loading" @click="loadMemories">
          <template #icon><RefreshCw :size="16" :class="{ spin: loading }" /></template>
          刷新
        </a-button>
        <a-button danger :disabled="!memories.length" :loading="clearing" @click="handleClearAll">
          <template #icon><Trash2 :size="16" /></template>
          清空全部
        </a-button>
      </div>
    </div>

    <a-spin :spinning="loading">
      <div v-if="!memories.length && !loading" class="memory-empty">
        <Brain :size="32" />
        <p>暂无记忆。开启「长期记忆」的智能体会在对话中自动记住你的偏好与背景。</p>
      </div>

      <div v-else class="memory-list">
        <div v-for="memory in memories" :key="memory.id" class="memory-item">
          <div class="memory-item-main">
            <p class="memory-content">{{ memory.content }}</p>
            <div class="memory-meta">
              <span class="memory-source" :class="memory.source">
                {{ memory.source === 'manual' ? '手动记住' : '自动抽取' }}
              </span>
              <span class="memory-time">{{ memory.created_at }}</span>
            </div>
          </div>
          <a-button
            class="lucide-icon-btn memory-delete-btn"
            type="text"
            :loading="deletingId === memory.id"
            @click="handleDelete(memory)"
            aria-label="删除记忆"
          >
            <template #icon><Trash2 :size="15" /></template>
          </a-button>
        </div>
      </div>
    </a-spin>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { Modal, message } from 'ant-design-vue'
import { Brain, RefreshCw, Trash2 } from 'lucide-vue-next'
import { memoryApi } from '@/apis/memory_api'

const loading = ref(false)
const clearing = ref(false)
const deletingId = ref(null)
const memories = ref([])

const loadMemories = async () => {
  loading.value = true
  try {
    const res = await memoryApi.list()
    memories.value = res.memories || []
  } catch (error) {
    message.error(error.message || '加载记忆失败')
  } finally {
    loading.value = false
  }
}

const handleDelete = async (memory) => {
  deletingId.value = memory.id
  try {
    await memoryApi.remove(memory.id)
    memories.value = memories.value.filter((item) => item.id !== memory.id)
    message.success('记忆已删除')
  } catch (error) {
    message.error(error.message || '删除记忆失败')
  } finally {
    deletingId.value = null
  }
}

const handleClearAll = () => {
  Modal.confirm({
    title: '清空全部记忆',
    content: `确定删除全部 ${memories.value.length} 条记忆吗？此操作不可恢复。`,
    okText: '清空',
    okType: 'danger',
    cancelText: '取消',
    onOk: async () => {
      clearing.value = true
      try {
        const res = await memoryApi.clear()
        memories.value = []
        message.success(`已清空 ${res.deleted} 条记忆`)
      } catch (error) {
        message.error(error.message || '清空记忆失败')
      } finally {
        clearing.value = false
      }
    }
  })
}

onMounted(loadMemories)
</script>

<style lang="less" scoped>
.memory-management {
  .header-actions {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-shrink: 0;
  }
}

.memory-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
  padding: 48px 16px;
  color: var(--gray-500);

  p {
    margin: 0;
    font-size: 13px;
    text-align: center;
    max-width: 360px;
    line-height: 1.6;
  }
}

.memory-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.memory-item {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 10px 12px;
  border: 1px solid var(--gray-150);
  border-radius: 8px;
  background: var(--gray-0);
  transition: border-color 0.2s;

  &:hover {
    border-color: var(--gray-300);

    .memory-delete-btn {
      opacity: 1;
    }
  }
}

.memory-item-main {
  flex: 1;
  min-width: 0;
}

.memory-content {
  margin: 0 0 6px;
  font-size: 14px;
  color: var(--gray-900);
  line-height: 1.5;
  word-break: break-word;
}

.memory-meta {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 12px;
  color: var(--gray-500);
}

.memory-source {
  padding: 1px 8px;
  border-radius: 999px;
  background: var(--gray-100);
  color: var(--gray-600);

  &.manual {
    background: rgba(4, 106, 130, 0.08);
    color: var(--main-700);
  }
}

.memory-delete-btn {
  flex-shrink: 0;
  color: var(--gray-500);
  opacity: 0;
  transition: opacity 0.2s;

  &:hover {
    color: var(--error-600, #dc2626);
  }
}
</style>
