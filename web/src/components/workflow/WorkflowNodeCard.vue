<template>
  <div class="wf-node" :class="{ selected }">
    <div class="wf-node-header">
      <div class="wf-node-icon" :style="{ background: meta.bg, color: meta.color }">
        <component :is="meta.icon" :size="14" />
      </div>
      <span class="wf-node-name">{{ data.name || meta.label }}</span>
      <span v-if="llmToolCount" class="wf-node-badge">{{ llmToolCount }} 工具</span>
    </div>

    <!-- llm / application-call / tool / kb-retrieval 摘要行 -->
    <div v-if="summary" class="wf-node-summary">{{ summary }}</div>

    <!-- condition 分支行：每个 case 一个 source handle -->
    <div v-if="type === 'condition'" class="wf-node-branches">
      <div v-for="(c, i) in cases" :key="i" class="wf-branch-row">
        <span class="wf-branch-label">IF</span>
        <span class="wf-branch-expr">{{ c.when || '（未填写条件）' }}</span>
        <Handle
          :id="`case-${i}`"
          type="source"
          :position="Position.Right"
          class="wf-branch-handle"
        />
      </div>
      <div class="wf-branch-row is-default">
        <span class="wf-branch-label">ELSE</span>
        <span class="wf-branch-expr">默认分支</span>
        <Handle id="default" type="source" :position="Position.Right" class="wf-branch-handle" />
      </div>
    </div>

    <!-- 入边 handle：start 节点没有 -->
    <Handle v-if="metaKey !== 'start'" type="target" :position="Position.Left" />
    <!-- 出边 handle：end 与 condition 节点没有（condition 用分支行 handle） -->
    <Handle
      v-if="metaKey !== 'end' && type !== 'condition'"
      type="source"
      :position="Position.Right"
    />
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { Handle, Position } from '@vue-flow/core'
import { NODE_META, metaKeyOf } from './nodeTypes'

const props = defineProps({
  id: { type: String, required: true },
  type: { type: String, required: true },
  data: { type: Object, required: true },
  selected: { type: Boolean, default: false }
})

const metaKey = computed(() => metaKeyOf(props.type, props.data.config))
const meta = computed(() => NODE_META[metaKey.value])
const cases = computed(() => props.data.config?.cases || [])

const summary = computed(() => {
  const config = props.data.config || {}
  if (props.type === 'llm') {
    return config.model || config.system_prompt || '未配置提示词'
  }
  if (props.type === 'application-call') {
    return config.target_agent_slug || '未选择智能体'
  }
  if (props.type === 'tool') {
    return config.tool_name || '未选择工具'
  }
  if (props.type === 'kb-retrieval') {
    const count = config.kb_ids?.length || 0
    return count > 0 ? `检索 ${count} 个知识库` : '检索全部可见知识库'
  }
  return ''
})

// llm 节点已挂工具时显示角标（内置工具数 + MCP 服务器数）
const llmToolCount = computed(() => {
  if (props.type !== 'llm') return 0
  const config = props.data.config || {}
  return (config.tools?.length || 0) + (config.mcps?.length || 0)
})
</script>

<style lang="less" scoped>
.wf-node {
  width: 220px;
  background: var(--gray-0);
  border: 1px solid var(--gray-200);
  border-radius: 8px;
  padding: 10px 12px;
  font-size: 13px;
  transition: border-color 0.2s;

  &.selected {
    border-color: var(--main-color);
  }

  &:hover {
    border-color: var(--main-color);
  }
}

.wf-node-header {
  display: flex;
  align-items: center;
  gap: 8px;
}

.wf-node-icon {
  width: 24px;
  height: 24px;
  border-radius: 6px;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}

.wf-node-name {
  font-weight: 600;
  color: var(--color-text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.wf-node-badge {
  flex-shrink: 0;
  margin-left: auto;
  font-size: 11px;
  line-height: 1;
  padding: 3px 6px;
  border-radius: 8px;
  background: var(--color-success-50);
  color: var(--color-success-700);
}

.wf-node-summary {
  margin-top: 6px;
  font-size: 12px;
  color: var(--color-text-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.wf-node-branches {
  margin-top: 8px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.wf-branch-row {
  position: relative;
  display: flex;
  align-items: center;
  gap: 6px;
  background: var(--gray-50);
  border-radius: 6px;
  padding: 4px 8px;
  font-size: 12px;

  &.is-default .wf-branch-label {
    color: var(--color-text-tertiary);
  }
}

.wf-branch-label {
  font-weight: 600;
  color: var(--color-warning-700);
  flex-shrink: 0;
}

.wf-branch-expr {
  font-family: monospace;
  color: var(--color-text-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.wf-branch-handle {
  // 分支行 handle 贴在行右侧（覆盖 vue-flow 默认绝对定位到节点边缘）
  position: absolute;
  right: -17px;
  top: 50%;
  transform: translateY(-50%);
}
</style>
