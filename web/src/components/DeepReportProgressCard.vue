<script setup>
import { computed } from 'vue'
import { Check, LoaderCircle, X, Minus } from 'lucide-vue-next'

// DeepReport 流水线进度卡片：消费 threadState.deepreportProgress
// （useAgentStreamHandler 按阶段/章节归并的 custom 进度事件）。
const props = defineProps({
  progress: {
    type: Object,
    required: true
  }
})

const STAGES = [
  { key: 'plan', label: '大纲规划' },
  { key: 'research', label: '章节研究' },
  { key: 'synthesize', label: '报告合成' },
  { key: 'citation_check', label: '引用回验' }
]

const chapterRows = computed(() => {
  const chapters = Object.values(props.progress.chapters || {})
  return chapters.sort((a, b) => (a.chapter_index || 0) - (b.chapter_index || 0))
})

const totalChapters = computed(() => {
  const planEvent = props.progress.stages?.plan
  return planEvent?.total_chapters || chapterRows.value[0]?.total_chapters || 0
})

const finishedChapters = computed(
  () => chapterRows.value.filter((ch) => ch.status === 'completed' || ch.status === 'failed').length
)

// research 阶段状态由章节事件推导（后端不发 research 阶段级事件）
const researchStatus = computed(() => {
  if (!chapterRows.value.length) return 'pending'
  if (totalChapters.value && finishedChapters.value >= totalChapters.value) return 'completed'
  return 'started'
})

const stageStatus = (key) => {
  if (key === 'research') return researchStatus.value
  return props.progress.stages?.[key]?.status || 'pending'
}

const stageMeta = (key) => {
  const event = props.progress.stages?.[key]
  if (key === 'plan' && event?.status === 'completed') {
    return `${event.total_chapters} 章`
  }
  if (key === 'research' && chapterRows.value.length) {
    return totalChapters.value ? `${finishedChapters.value}/${totalChapters.value}` : ''
  }
  if (key === 'citation_check' && event?.status === 'completed') {
    return `引用 ${event.cited} 条 · 回验通过 ${event.verified}`
  }
  return ''
}

const statusIcon = (status) => {
  if (status === 'completed') return Check
  if (status === 'failed') return X
  if (status === 'started') return LoaderCircle
  return Minus
}
</script>

<template>
  <div class="deepreport-progress-card">
    <template v-for="stage in STAGES" :key="stage.key">
      <div class="dr-stage-row" :class="`is-${stageStatus(stage.key)}`">
        <component
          :is="statusIcon(stageStatus(stage.key))"
          :size="14"
          class="dr-status-icon"
          :class="{ 'is-spinning': stageStatus(stage.key) === 'started' }"
        />
        <span class="dr-stage-label">{{ stage.label }}</span>
        <span v-if="stageMeta(stage.key)" class="dr-stage-meta">{{ stageMeta(stage.key) }}</span>
      </div>
      <div v-if="stage.key === 'research' && chapterRows.length" class="dr-chapters">
        <div
          v-for="chapter in chapterRows"
          :key="chapter.chapter_id"
          class="dr-chapter-row"
          :class="`is-${chapter.status}`"
        >
          <component
            :is="statusIcon(chapter.status)"
            :size="12"
            class="dr-status-icon"
            :class="{ 'is-spinning': chapter.status === 'started' }"
          />
          <span class="dr-chapter-heading">{{ chapter.heading }}</span>
          <span v-if="chapter.status === 'completed'" class="dr-stage-meta">
            {{ chapter.facts_count }} 条事实
          </span>
          <span v-else-if="chapter.status === 'failed'" class="dr-stage-meta is-error">失败</span>
        </div>
      </div>
    </template>
  </div>
</template>

<style lang="less" scoped>
.deepreport-progress-card {
  margin: 8px 0;
  padding: 12px 16px;
  border: 1px solid var(--gray-200);
  border-radius: 12px;
  background: var(--gray-25);
  font-size: 13px;
  color: var(--gray-800);
}

.dr-stage-row,
.dr-chapter-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 3px 0;

  &.is-pending {
    color: var(--gray-500);
  }
}

.dr-chapters {
  margin: 2px 0 2px 22px;
  padding-left: 8px;
  border-left: 1px solid var(--gray-200);
}

.dr-chapter-row {
  font-size: 12px;
  color: var(--gray-600);
}

.dr-status-icon {
  flex-shrink: 0;
  color: var(--gray-400);

  .is-completed & {
    color: var(--main-600);
  }

  .is-failed & {
    color: var(--color-error-500);
  }

  &.is-spinning {
    color: var(--main-600);
    animation: dr-spin 1s linear infinite;
  }
}

.dr-stage-label {
  font-weight: 500;
}

.dr-chapter-heading {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.dr-stage-meta {
  margin-left: auto;
  flex-shrink: 0;
  font-size: 12px;
  color: var(--gray-500);

  &.is-error {
    color: var(--color-error-500);
  }
}

@keyframes dr-spin {
  to {
    transform: rotate(360deg);
  }
}
</style>
