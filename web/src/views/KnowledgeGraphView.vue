<template>
  <div class="knowledge-graph-view layout-container">
    <PageHeader :title="headerTitle" :loading="graph.fetching" :show-border="true">
      <template #actions>
        <a-button class="back-btn" @click="goBack">
          <ArrowLeft :size="16" />
          返回工作区
        </a-button>
      </template>
    </PageHeader>

    <div class="graph-body">
      <ResourceEmptyState
        v-if="loadError"
        class="graph-empty-state"
        :title="loadErrorTitle"
        :description="loadErrorDescription"
        :icon="Network"
        full-height
      >
        <template #actions>
          <a-button class="lucide-icon-btn" @click="goBack">
            <ArrowLeft :size="16" />
            返回工作区
          </a-button>
        </template>
      </ResourceEmptyState>

      <div v-else class="graph-wrapper">
        <GraphCanvas
          ref="graphRef"
          :graph-data="graph.graphData"
          @node-click="graph.handleNodeClick"
          @edge-click="graph.handleEdgeClick"
          @canvas-click="graph.handleCanvasClick"
        >
          <template #top>
            <div class="compact-actions">
              <div class="actions-left">
                <a-input
                  v-model:value="searchInput"
                  placeholder="搜索实体"
                  style="width: 240px"
                  allow-clear
                  @keydown.enter="onSearch"
                >
                  <template #suffix>
                    <component
                      :is="graph.fetching ? Loader2 : Search"
                      :size="14"
                      class="search-suffix-icon"
                      :class="{ spin: graph.fetching }"
                      @click="onSearch"
                    />
                  </template>
                </a-input>
                <a-button class="action-btn" title="刷新" @click="loadGraph">
                  <RefreshCw :size="16" :class="{ spin: graph.fetching }" />
                </a-button>
              </div>
              <div class="actions-right">
                <a-button class="action-btn" title="设置" @click="toggleSettingsPanel">
                  <Settings :size="16" />
                </a-button>
              </div>
            </div>
          </template>
        </GraphCanvas>

        <ResourceEmptyState
          v-if="showGraphDataEmpty"
          class="graph-empty-state"
          :title="graphDataEmptyTitle"
          :description="graphDataEmptyDescription"
          :icon="Network"
          full-height
        >
          <template #actions>
            <a-button v-if="searchInput.trim()" class="lucide-icon-btn" @click="clearGraphSearch">
              <Search :size="16" />
              清空搜索
            </a-button>
            <a-button v-else class="lucide-icon-btn" @click="loadGraph">
              <RefreshCw :size="16" :class="{ spin: graph.fetching }" />
              刷新图谱
            </a-button>
          </template>
        </ResourceEmptyState>

        <GraphDetailPanel
          :visible="graph.showDetailDrawer"
          :item="graph.selectedItem"
          :type="graph.selectedItemType"
          @close="graph.handleCanvasClick"
        />

        <transition name="slide-fade">
          <div v-if="showSettings" class="floating-panel settings-panel">
            <div class="panel-header">
              <span class="panel-title">图谱设置</span>
            </div>
            <div class="panel-body">
              <a-form layout="vertical">
                <a-form-item label="最大节点数 (limit)">
                  <a-input-number
                    v-model:value="subgraphParams.maxNodes"
                    :min="10"
                    :max="1000"
                    :step="10"
                    style="width: 100%"
                  />
                </a-form-item>
                <a-form-item label="搜索深度 (depth)">
                  <a-input-number
                    v-model:value="subgraphParams.maxDepth"
                    :min="1"
                    :max="5"
                    :step="1"
                    style="width: 100%"
                  />
                </a-form-item>
                <a-form-item label="排除 Chunk 节点">
                  <a-switch v-model:checked="subgraphParams.excludeChunk" />
                </a-form-item>
                <a-form-item>
                  <a-button type="primary" style="width: 100%" @click="applySettings">
                    应用
                  </a-button>
                </a-form-item>
              </a-form>
            </div>
          </div>
        </transition>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import { ArrowLeft, Loader2, Network, RefreshCw, Search, Settings } from 'lucide-vue-next'
import PageHeader from '@/components/shared/PageHeader.vue'
import GraphCanvas from '@/components/GraphCanvas.vue'
import GraphDetailPanel from '@/components/GraphDetailPanel.vue'
import ResourceEmptyState from '@/components/shared/ResourceEmptyState.vue'
import { graphApi } from '@/apis/graph_api'
import { useGraph } from '@/composables/useGraph'

const route = useRoute()
const router = useRouter()

const kbId = computed(() => route.params.kbId)
const kbName = ref(route.query.name || '')

const graphRef = ref(null)
const graph = reactive(useGraph(graphRef))
const graphLoaded = ref(false)
const searchInput = ref('')
const showSettings = ref(false)
const loadError = ref('')

const subgraphParams = reactive({
  maxNodes: 100,
  maxDepth: 2,
  excludeChunk: true
})

let graphLoadRequestSeq = 0

const headerTitle = computed(() =>
  kbName.value ? `${kbName.value} · 知识图谱` : '知识图谱'
)

const hasGraphNodes = computed(() => graph.graphData.nodes.length > 0)

const showGraphDataEmpty = computed(
  () => !loadError.value && graphLoaded.value && !graph.fetching && !hasGraphNodes.value
)

const graphDataEmptyTitle = computed(() =>
  searchInput.value.trim() ? '未找到匹配实体' : '暂无知识图谱'
)
const graphDataEmptyDescription = computed(() => {
  if (searchInput.value.trim()) return '换个关键词或调整图谱设置后再搜索。'
  return '当前知识库还没有可展示的实体与关系。'
})

const loadErrorTitle = computed(() =>
  loadError.value === 'unsupported' ? '知识图谱不可用' : '无法访问知识图谱'
)
const loadErrorDescription = computed(() =>
  loadError.value === 'unsupported'
    ? '仅 Milvus 类型的知识库支持知识图谱功能。'
    : '你没有权限访问该知识库，或知识库不存在。'
)

const goBack = () => {
  router.push('/workspace')
}

const toggleSettingsPanel = () => {
  showSettings.value = !showSettings.value
}

const loadGraph = async () => {
  if (!kbId.value) return

  const requestSeq = ++graphLoadRequestSeq
  const currentKbId = kbId.value
  graph.fetching = true
  loadError.value = ''
  if (!hasGraphNodes.value) {
    graphLoaded.value = false
  }
  try {
    const res = await graphApi.getSubgraph({
      kb_id: currentKbId,
      node_label: searchInput.value || '*',
      max_nodes: subgraphParams.maxNodes,
      max_depth: subgraphParams.maxDepth,
      exclude_chunk: subgraphParams.excludeChunk
    })

    if (requestSeq === graphLoadRequestSeq && currentKbId === kbId.value && res.success && res.data) {
      graph.updateGraphData(res.data.nodes, res.data.edges)
    }
  } catch (e) {
    if (requestSeq !== graphLoadRequestSeq || currentKbId !== kbId.value) return
    const status = e?.response?.status
    if (status === 404) {
      loadError.value = 'unsupported'
    } else if (status === 403) {
      loadError.value = 'forbidden'
    } else {
      console.error('Failed to load graph:', e)
      message.error('加载图谱失败')
    }
  } finally {
    if (requestSeq === graphLoadRequestSeq) {
      graph.fetching = false
      graphLoaded.value = true
    }
  }
}

const applySettings = () => {
  showSettings.value = false
  loadGraph()
}

const onSearch = () => {
  loadGraph()
}

const clearGraphSearch = () => {
  searchInput.value = ''
  loadGraph()
}

watch(kbId, () => {
  graphLoadRequestSeq += 1
  graphLoaded.value = false
  loadError.value = ''
  graph.clearGraph()
  kbName.value = route.query.name || ''
  loadGraph()
})

onMounted(() => {
  loadGraph()
})

onUnmounted(() => {
  graphLoadRequestSeq += 1
})
</script>

<style scoped lang="less">
.knowledge-graph-view {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
}

.back-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.graph-body {
  position: relative;
  flex: 1;
  min-height: 0;
  overflow: hidden;
  background: var(--gray-0);
}

.graph-wrapper {
  height: 100%;
  width: 100%;
  position: relative;
}

.graph-empty-state {
  position: absolute;
  inset: 0;
  z-index: 30;
  pointer-events: none;

  :deep(.resource-empty-state__actions) {
    pointer-events: auto;
  }
}

.compact-actions {
  position: absolute;
  top: 10px;
  left: 10px;
  right: 10px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  pointer-events: none;

  .actions-left,
  .actions-right {
    pointer-events: auto;
    display: flex;
    align-items: center;
    gap: 4px;
    background: var(--color-trans-light);
    backdrop-filter: blur(12px);
    padding: 2px;
    border-radius: 8px;
    box-shadow: 0 0 4px 0px var(--shadow-2);
    border: 1px solid var(--gray-100);
  }

  :deep(.ant-input-affix-wrapper) {
    padding: 4px 11px;
    border-radius: 6px;
    border-color: transparent;
    box-shadow: none;
    background: var(--color-trans-light);

    &:hover,
    &:focus,
    &-focused {
      background: var(--main-0);
      border-color: var(--primary-color);
    }

    input {
      background: transparent;
    }
  }

  .action-btn {
    width: 32px;
    height: 32px;
    padding: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    border: none;
    background: transparent;
    color: var(--gray-600);
    border-radius: 6px;
    box-shadow: none;

    &:hover {
      background: var(--shadow-1);
      color: var(--primary-color);
    }
  }

  .search-suffix-icon {
    cursor: pointer;
  }

  .spin {
    animation: spin 1s linear infinite;
  }
}

.floating-panel {
  position: absolute;
  top: 60px;
  right: 10px;
  width: 300px;
  max-height: calc(100% - 60px);
  overflow-y: auto;
  z-index: 100;
  background: var(--color-trans-light);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  border-radius: 8px;
  border: 1px solid var(--gray-100);
  box-shadow: 0 0 4px 0px var(--shadow-2);
  font-size: 13px;

  .panel-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 10px 14px;
    border-bottom: 1px solid var(--gray-200);

    .panel-title {
      font-size: 13px;
      font-weight: 600;
      color: var(--gray-1000);
    }
  }

  .panel-body {
    padding: 10px 14px;
  }
}

.slide-fade-enter-active {
  transition: all 0.25s ease-out;
}

.slide-fade-leave-active {
  transition: all 0.2s cubic-bezier(1, 0.5, 0.8, 1);
}

.slide-fade-enter-from,
.slide-fade-leave-to {
  transform: translateX(20px);
  opacity: 0;
}
</style>
