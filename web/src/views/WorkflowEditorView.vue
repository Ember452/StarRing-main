<template>
  <div class="workflow-editor-view">
    <!-- 顶栏 -->
    <div class="editor-topbar">
      <div class="topbar-left">
        <a-button type="text" class="back-btn" @click="goBack">
          <template #icon><ArrowLeft :size="18" /></template>
        </a-button>
        <a-input
          v-model:value="wfMeta.name"
          class="wf-name-input"
          :bordered="false"
          placeholder="工作流名称"
        />
        <a-tag v-if="validation" :color="validation.valid ? 'green' : 'red'" class="validate-tag">
          {{ validation.valid ? '校验通过' : '校验失败' }}
        </a-tag>
      </div>
      <div class="topbar-right">
        <a-button @click="runValidate">
          <template #icon><ShieldCheck :size="16" /></template>
          校验
        </a-button>
        <a-button @click="openJsonDrawer">
          <template #icon><Braces :size="16" /></template>
          JSON
        </a-button>
        <a-button type="primary" :loading="saving" @click="save">
          <template #icon><Save :size="16" /></template>
          保存
        </a-button>
      </div>
    </div>

    <div class="editor-body">
      <!-- 左侧节点面板 -->
      <div class="node-palette">
        <div class="palette-title">节点</div>
        <div
          v-for="(meta, key) in NODE_META"
          :key="key"
          class="palette-item"
          :class="{ disabled: paletteDisabled(key) }"
          draggable="true"
          @dragstart="onDragStart($event, key)"
          @click="addNodeByClick(key)"
        >
          <div class="palette-icon" :style="{ background: meta.bg, color: meta.color }">
            <component :is="meta.icon" :size="16" />
          </div>
          <span class="palette-label">{{ meta.label }}</span>
        </div>
        <div class="palette-tip">拖拽或点击添加到画布</div>
      </div>

      <!-- 画布 -->
      <div class="canvas-wrap" @drop="onDrop" @dragover.prevent>
        <VueFlow
          v-model:nodes="nodes"
          v-model:edges="edges"
          :node-types="nodeTypes"
          :default-edge-options="{ type: 'smoothstep' }"
          :delete-key-code="['Backspace', 'Delete']"
          :min-zoom="0.2"
          :max-zoom="2"
          fit-view-on-init
          @connect="onConnect"
          @node-click="onNodeClick"
          @pane-click="onPaneClick"
        >
          <Background pattern-color="#c8cdcd" :gap="18" :size="1.2" />
          <Controls position="bottom-left" />
          <MiniMap position="bottom-right" pannable zoomable />
        </VueFlow>
      </div>

      <!-- 右侧配置面板 -->
      <div v-if="selectedNode" class="config-panel">
        <div class="panel-header">
          <span class="panel-title">{{ selectedMeta?.label }}</span>
          <a-button type="text" size="small" @click="selectedNodeId = null">
            <template #icon><X :size="16" /></template>
          </a-button>
        </div>

        <div class="panel-body">
          <div class="field">
            <div class="field-label">节点名称</div>
            <a-input v-model:value="selectedNode.data.name" placeholder="节点展示名" />
          </div>

          <!-- start / end -->
          <template v-if="selectedNode.type === 'start-end'">
            <div class="field">
              <div class="field-label">类型</div>
              <a-input
                :value="selectedNode.data.config.kind === 'start' ? '开始节点' : '结束节点'"
                disabled
              />
            </div>
            <div v-if="selectedNode.data.config.kind === 'end'" class="field">
              <div class="field-label">输出模板</div>
              <a-textarea
                v-model:value="selectedNode.data.config.input_template"
                :rows="3"
                placeholder="可选，如 {{ last_output }}"
              />
            </div>
          </template>

          <!-- llm -->
          <template v-else-if="selectedNode.type === 'llm'">
            <div class="field">
              <div class="field-label required">System Prompt</div>
              <a-textarea
                v-model:value="selectedNode.data.config.system_prompt"
                :rows="6"
                placeholder="该节点的系统提示词"
              />
            </div>
            <div class="field">
              <div class="field-label">模型</div>
              <a-input
                v-model:value="selectedNode.data.config.model"
                placeholder="可选，留空使用默认模型"
              />
            </div>
            <div class="field">
              <div class="field-label">输入模板</div>
              <a-textarea
                v-model:value="selectedNode.data.config.input_template"
                :rows="3"
                placeholder="可选，如 {{ query }}"
              />
            </div>
            <div class="field">
              <div class="field-label">挂载工具</div>
              <a-select
                v-model:value="selectedNode.data.config.tools"
                :options="toolSelectOptions"
                mode="multiple"
                placeholder="可选，选择内置工具"
                style="width: 100%"
              />
            </div>
            <div class="field">
              <div class="field-label">挂载 MCP</div>
              <a-select
                v-model:value="selectedNode.data.config.mcps"
                :options="mcpSelectOptions"
                mode="multiple"
                placeholder="可选，选择 MCP 服务器"
                style="width: 100%"
              />
            </div>
            <div v-if="llmToolConfigured" class="field">
              <div class="field-label">最大工具步数</div>
              <a-input-number
                v-model:value="selectedNode.data.config.max_tool_steps"
                :min="1"
                :max="25"
                placeholder="默认 10"
                style="width: 100%"
              />
            </div>
          </template>

          <!-- condition -->
          <template v-else-if="selectedNode.type === 'condition'">
            <div class="field">
              <div class="field-label required">分支条件</div>
              <div v-for="(c, i) in selectedNode.data.config.cases" :key="i" class="case-row">
                <span class="case-index">IF</span>
                <a-input v-model:value="c.when" class="case-expr" placeholder="条件表达式" />
                <a-button
                  type="text"
                  size="small"
                  danger
                  :disabled="selectedNode.data.config.cases.length <= 1"
                  @click="removeCase(i)"
                >
                  <template #icon><Trash2 :size="14" /></template>
                </a-button>
              </div>
              <a-button type="dashed" block size="small" @click="addCase">
                <template #icon><Plus :size="14" /></template>
                添加分支
              </a-button>
              <div class="field-tip">
                分支目标通过画布连线指定，未命中任何分支时走 ELSE 默认分支
              </div>
            </div>
          </template>

          <!-- application-call -->
          <template v-else-if="selectedNode.type === 'application-call'">
            <div class="field">
              <div class="field-label required">目标智能体</div>
              <a-select
                v-model:value="selectedNode.data.config.target_agent_slug"
                :options="agentOptions"
                show-search
                placeholder="选择要调用的智能体"
                style="width: 100%"
              />
            </div>
            <div class="field">
              <div class="field-label">输入模板</div>
              <a-textarea
                v-model:value="selectedNode.data.config.input_template"
                :rows="3"
                placeholder="可选，如 {{ last_output }}"
              />
            </div>
          </template>

          <!-- tool -->
          <template v-else-if="selectedNode.type === 'tool'">
            <div class="field">
              <div class="field-label required">工具来源</div>
              <a-radio-group
                :value="selectedNode.data.config.tool_source"
                @change="onToolSourceChange($event.target.value)"
              >
                <a-radio-button value="buildin">内置工具</a-radio-button>
                <a-radio-button value="mcp">MCP 工具</a-radio-button>
              </a-radio-group>
            </div>
            <div v-if="selectedNode.data.config.tool_source === 'mcp'" class="field">
              <div class="field-label required">MCP 服务器</div>
              <a-select
                :value="selectedNode.data.config.mcp_server || undefined"
                :options="mcpSelectOptions"
                show-search
                placeholder="选择 MCP 服务器"
                style="width: 100%"
                @change="onMcpServerChange"
              />
            </div>
            <div class="field">
              <div class="field-label required">工具</div>
              <a-select
                v-model:value="selectedNode.data.config.tool_name"
                :options="toolNameOptions"
                :loading="mcpToolsLoading"
                show-search
                placeholder="选择工具"
                style="width: 100%"
              />
            </div>
            <div class="field">
              <div class="field-label">调用参数（JSON）</div>
              <a-textarea
                v-model:value="toolArgsText"
                class="args-textarea"
                :rows="5"
                placeholder='{"city": "{{ node_outputs["start"].summary }}"}'
                @blur="commitToolArgs"
              />
              <div class="field-tip">
                参数值为字面量，或 {{ toolArgsExprExample }} 这类整串表达式引用上游输出
              </div>
            </div>
          </template>

          <!-- kb-retrieval -->
          <template v-else-if="selectedNode.type === 'kb-retrieval'">
            <div class="field">
              <div class="field-label required">检索内容</div>
              <a-textarea
                v-model:value="selectedNode.data.config.query"
                :rows="3"
                placeholder="检索文本，支持内嵌表达式引用上游输出"
              />
              <div class="field-tip">支持 {{ toolArgsExprExample }} 这类内嵌表达式引用上游输出</div>
            </div>
            <div class="field">
              <div class="field-label">知识库</div>
              <a-select
                v-model:value="selectedNode.data.config.kb_ids"
                :options="kbSelectOptions"
                mode="multiple"
                placeholder="可选，留空检索全部可见知识库"
                style="width: 100%"
              />
            </div>
            <div class="field">
              <div class="field-label">每库返回条数</div>
              <a-input-number
                v-model:value="selectedNode.data.config.top_k"
                :min="1"
                :max="50"
                placeholder="默认 5"
                style="width: 100%"
              />
            </div>
          </template>

          <!-- human-review -->
          <template v-else-if="selectedNode.type === 'human-review'">
            <div class="field">
              <div class="field-label required">审核提示语</div>
              <a-textarea
                v-model:value="selectedNode.data.config.message"
                :rows="3"
                placeholder="审核提示语，支持内嵌表达式引用上游输出"
              />
              <div class="field-tip">
                运行到此节点时将暂停等待人工审核，消息支持 {{ toolArgsExprExample }} 这类内嵌表达式。
              </div>
            </div>
          </template>

          <!-- 高级：节点级重试（非 start-end 节点通用） -->
          <a-collapse v-if="selectedNode.type !== 'start-end'" ghost class="advanced-collapse">
            <a-collapse-panel key="advanced" header="高级">
              <div class="field">
                <div class="field-label">失败重试次数</div>
                <a-input-number
                  v-model:value="selectedNode.data.config.retry_count"
                  :min="0"
                  :max="5"
                  placeholder="默认 0 不重试"
                  style="width: 100%"
                />
              </div>
              <div class="field">
                <div class="field-label">重试间隔（秒）</div>
                <a-input-number
                  v-model:value="selectedNode.data.config.retry_interval"
                  :min="0"
                  :max="60"
                  placeholder="默认 1"
                  style="width: 100%"
                />
                <div class="field-tip">重试超限后节点仍按失败处理，不会跳过</div>
              </div>
            </a-collapse-panel>
          </a-collapse>

          <a-button danger block class="delete-node-btn" @click="removeSelectedNode">
            删除节点
          </a-button>
        </div>
      </div>
    </div>

    <!-- JSON 抽屉 -->
    <a-drawer v-model:open="jsonDrawerOpen" title="工作流定义 JSON" width="560" placement="right">
      <a-textarea v-model:value="jsonText" class="json-textarea" :rows="26" />
      <div class="drawer-footer">
        <a-button @click="jsonDrawerOpen = false">取消</a-button>
        <a-button type="primary" :loading="jsonApplying" @click="applyJson">校验并应用</a-button>
      </div>
    </a-drawer>
  </div>
</template>

<script setup>
import { ref, reactive, computed, markRaw, nextTick, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import { VueFlow, useVueFlow } from '@vue-flow/core'
import { Background } from '@vue-flow/background'
import { Controls } from '@vue-flow/controls'
import { MiniMap } from '@vue-flow/minimap'
import { ArrowLeft, Save, Braces, ShieldCheck, X, Plus, Trash2 } from 'lucide-vue-next'
import { workflowApi } from '@/apis/workflow_api'
import { agentApi } from '@/apis/agent_api'
import { mcpApi } from '@/apis/mcp_api'
import WorkflowNodeCard from '@/components/workflow/WorkflowNodeCard.vue'
import { NODE_META, metaKeyOf, createFlowNode } from '@/components/workflow/nodeTypes'
import { definitionToFlow, flowToDefinition } from '@/components/workflow/serialize'

import '@vue-flow/core/dist/style.css'
import '@vue-flow/core/dist/theme-default.css'
import '@vue-flow/controls/dist/style.css'
import '@vue-flow/minimap/dist/style.css'

const route = useRoute()
const router = useRouter()
const workflowId = route.params.workflowId

const nodeCard = markRaw(WorkflowNodeCard)
const nodeTypes = {
  'start-end': nodeCard,
  llm: nodeCard,
  condition: nodeCard,
  'application-call': nodeCard,
  tool: nodeCard,
  'kb-retrieval': nodeCard,
  'human-review': nodeCard
}

const { screenToFlowCoordinate, getViewport, setViewport, fitView, removeNodes, addEdges } =
  useVueFlow()

// ---------------------------------------------------------------------------
// 数据加载
// ---------------------------------------------------------------------------
const nodes = ref([])
const edges = ref([])
const wfMeta = reactive({ name: '', desc: '', version: 1 })

async function loadWorkflow() {
  try {
    const wf = await workflowApi.detail(workflowId)
    wfMeta.name = wf.name
    wfMeta.desc = wf.desc || ''
    wfMeta.version = wf.definition?.version || 1
    const flow = definitionToFlow(wf.definition)
    if (!flow.nodes.length) {
      // 空工作流：自动放置 start + end
      flow.nodes = [
        createFlowNode('start', { x: 100, y: 220 }),
        createFlowNode('end', { x: 560, y: 220 })
      ]
    }
    nodes.value = flow.nodes
    edges.value = flow.edges
    if (wf.definition?.viewport) {
      setViewport(wf.definition.viewport)
    } else {
      await nextTick()
      fitView({ padding: 0.2 })
    }
  } catch (err) {
    message.error(err.message || '加载工作流失败')
    router.push('/workflows')
  }
}

// application-call 智能体下拉
const agentOptions = ref([])
async function loadAgents() {
  try {
    const data = await agentApi.getAgents()
    agentOptions.value = (data.agents || [])
      // 工作流不允许嵌套调用工作流（后端按 context_schema 拒绝），下拉直接过滤
      .filter((a) => a.backend_id !== 'WorkflowBackend')
      .map((a) => {
        const slug = a.slug || a.agent_id || a.id
        return { value: slug, label: a.name ? `${a.name}（${slug}）` : slug }
      })
  } catch {
    // 智能体列表加载失败不阻塞编辑器，下拉为空时用户可手动排查
    agentOptions.value = []
  }
}

// ---------------------------------------------------------------------------
// 工具 / MCP / 知识库选项（tool 节点 + llm 节点挂工具 + kb-retrieval 节点共用）
// ---------------------------------------------------------------------------
const toolSelectOptions = ref([])
const mcpSelectOptions = ref([])
const kbSelectOptions = ref([])

async function loadResourceOptions() {
  try {
    const data = await workflowApi.resourceOptions()
    toolSelectOptions.value = (data.tools || []).map((t) => ({ value: t.key, label: t.name }))
    mcpSelectOptions.value = (data.mcps || []).map((s) => ({ value: s.key, label: s.name }))
    kbSelectOptions.value = (data.knowledges || []).map((k) => ({ value: k.key, label: k.name }))
  } catch {
    // 选项加载失败不阻塞编辑器，下拉为空时用户可手动排查
    toolSelectOptions.value = []
    mcpSelectOptions.value = []
    kbSelectOptions.value = []
  }
}

// llm 节点：已挂工具/MCP 时才展示步数限制输入
const llmToolConfigured = computed(() => {
  const config = selectedNode.value?.data.config || {}
  return (config.tools?.length || 0) + (config.mcps?.length || 0) > 0
})

// tool 节点：MCP 工具按服务器懒加载（缓存，避免切换节点重复拉取）
const mcpToolsCache = reactive({})
const mcpToolsLoading = ref(false)

async function loadMcpTools(serverSlug) {
  if (!serverSlug || mcpToolsCache[serverSlug]) return
  mcpToolsLoading.value = true
  try {
    const result = await mcpApi.getMcpServerEnabledTools(serverSlug)
    mcpToolsCache[serverSlug] = (result?.data || []).map((t) => ({ value: t.name, label: t.name }))
  } catch (err) {
    message.error(err.message || `加载 MCP 服务器 ${serverSlug} 的工具失败`)
  } finally {
    mcpToolsLoading.value = false
  }
}

const toolNameOptions = computed(() => {
  const config = selectedNode.value?.data.config || {}
  if (config.tool_source === 'mcp') {
    return mcpToolsCache[config.mcp_server] || []
  }
  return toolSelectOptions.value
})

function onToolSourceChange(source) {
  const config = selectedNode.value.data.config
  if (config.tool_source === source) return
  // 切换来源后原工具/服务器选择不再有效，清空关联字段
  config.tool_source = source
  config.tool_name = ''
  config.mcp_server = ''
}

function onMcpServerChange(serverSlug) {
  const config = selectedNode.value.data.config
  config.mcp_server = serverSlug
  config.tool_name = ''
  loadMcpTools(serverSlug)
}

// tool 节点 args：面板内用文本态编辑，blur 时 JSON.parse 校验后写回 config
const toolArgsText = ref('')
// 提示文案里的 {{ }} 示例不能直接写在模板插值中（}} 会提前终止插值），放常量
const toolArgsExprExample = '{{ node_outputs["n1"].summary }}'

function commitToolArgs() {
  const node = selectedNode.value
  if (!node || node.type !== 'tool') return
  let parsed
  try {
    parsed = JSON.parse(toolArgsText.value || '{}')
  } catch {
    message.error('调用参数不是合法的 JSON，未保存')
    return
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    message.error('调用参数必须是 JSON 对象')
    return
  }
  node.data.config.args = parsed
}

onMounted(() => {
  loadWorkflow()
  loadAgents()
  loadResourceOptions()
})

// ---------------------------------------------------------------------------
// 节点面板：拖拽 / 点击添加
// ---------------------------------------------------------------------------
function hasStartEnd(kind) {
  return nodes.value.some((n) => n.type === 'start-end' && n.data.config.kind === kind)
}

function paletteDisabled(metaKey) {
  if (metaKey === 'start') return hasStartEnd('start')
  if (metaKey === 'end') return hasStartEnd('end')
  return false
}

function checkAddable(metaKey) {
  if (paletteDisabled(metaKey)) {
    message.warning(`${NODE_META[metaKey].label}节点只能有一个`)
    return false
  }
  if (nodes.value.length >= 50) {
    message.warning('工作流节点数已达上限 50')
    return false
  }
  return true
}

function onDragStart(event, metaKey) {
  if (paletteDisabled(metaKey)) {
    event.preventDefault()
    return
  }
  event.dataTransfer.setData('application/workflow-node', metaKey)
  event.dataTransfer.effectAllowed = 'move'
}

function onDrop(event) {
  const metaKey = event.dataTransfer.getData('application/workflow-node')
  if (!metaKey || !checkAddable(metaKey)) return
  const position = screenToFlowCoordinate({ x: event.clientX, y: event.clientY })
  nodes.value.push(createFlowNode(metaKey, position))
}

function addNodeByClick(metaKey) {
  if (!checkAddable(metaKey)) return
  // 点击添加：放在当前视口中心附近，带随机偏移避免完全重叠
  const center = screenToFlowCoordinate({
    x: window.innerWidth / 2,
    y: window.innerHeight / 2
  })
  nodes.value.push(
    createFlowNode(metaKey, {
      x: center.x + Math.random() * 60 - 30,
      y: center.y + Math.random() * 60 - 30
    })
  )
}

// ---------------------------------------------------------------------------
// 连线规则
// ---------------------------------------------------------------------------
function onConnect(connection) {
  if (connection.source === connection.target) {
    message.warning('不允许自环连线')
    return
  }
  const sourceNode = nodes.value.find((n) => n.id === connection.source)
  if (!sourceNode) return
  if (sourceNode.type === 'condition') {
    // condition：每个分支 handle 只允许一条出边
    const exists = edges.value.some(
      (e) => e.source === connection.source && e.sourceHandle === connection.sourceHandle
    )
    if (exists) {
      message.warning('该分支已有连线，请先删除原有连线')
      return
    }
  } else {
    // 普通节点：只允许一条出边（与后端 fail-fast 规则对齐）
    const exists = edges.value.some((e) => e.source === connection.source)
    if (exists) {
      message.warning('该节点已有出边，普通节点只允许一条出边')
      return
    }
  }
  if (edges.value.length >= 100) {
    message.warning('工作流边数已达上限 100')
    return
  }
  addEdges([connection])
}

// ---------------------------------------------------------------------------
// 选中与配置面板
// ---------------------------------------------------------------------------
const selectedNodeId = ref(null)
const selectedNode = computed(() => nodes.value.find((n) => n.id === selectedNodeId.value) || null)
const selectedMeta = computed(() =>
  selectedNode.value
    ? NODE_META[metaKeyOf(selectedNode.value.type, selectedNode.value.data.config)]
    : null
)

// 选中 tool 节点时同步 args 文本态并预加载 MCP 工具列表
// （watch 的 getter 在注册时即同步执行，必须放在 selectedNode 定义之后）
watch(
  () => selectedNode.value,
  (node) => {
    if (node?.type === 'tool') {
      toolArgsText.value = JSON.stringify(node.data.config.args || {}, null, 2)
      if (node.data.config.tool_source === 'mcp') loadMcpTools(node.data.config.mcp_server)
    }
  }
)

function onNodeClick({ node }) {
  selectedNodeId.value = node.id
}

function onPaneClick() {
  selectedNodeId.value = null
}

function removeSelectedNode() {
  if (!selectedNode.value) return
  removeNodes([selectedNode.value.id])
  selectedNodeId.value = null
}

// condition case 增删（删除时同步清理/重排对应分支连线）
function addCase() {
  selectedNode.value.data.config.cases.push({ when: '', then: null })
}

function removeCase(index) {
  const nodeId = selectedNode.value.id
  selectedNode.value.data.config.cases.splice(index, 1)
  edges.value = edges.value
    .filter((e) => !(e.source === nodeId && e.sourceHandle === `case-${index}`))
    .map((e) => {
      if (e.source === nodeId && e.sourceHandle?.startsWith('case-')) {
        const i = Number(e.sourceHandle.slice(5))
        if (i > index) return { ...e, sourceHandle: `case-${i - 1}` }
      }
      return e
    })
}

// ---------------------------------------------------------------------------
// 校验 / 保存 / JSON
// ---------------------------------------------------------------------------
const validation = ref(null)
const saving = ref(false)

function buildDefinition() {
  return flowToDefinition(nodes.value, edges.value, {
    version: wfMeta.version,
    viewport: getViewport()
  })
}

async function runValidate() {
  try {
    const result = await workflowApi.validateDefinition(buildDefinition())
    validation.value = result
    if (result.valid) {
      message.success(`校验通过：${result.node_count} 个节点，${result.edge_count} 条边`)
    } else {
      message.error(result.error)
    }
    return result.valid
  } catch (err) {
    message.error(err.message || '校验请求失败')
    return false
  }
}

async function save() {
  if (!wfMeta.name) {
    message.warning('工作流名称不能为空')
    return
  }
  saving.value = true
  try {
    const definition = buildDefinition()
    const result = await workflowApi.validateDefinition(definition)
    validation.value = result
    if (!result.valid) {
      message.error(`定义校验失败：${result.error}`)
      return
    }
    const wf = await workflowApi.update(workflowId, {
      name: wfMeta.name,
      definition
    })
    wfMeta.version = wf.definition?.version || wfMeta.version
    message.success('保存成功')
  } catch (err) {
    message.error(err.message || '保存失败')
  } finally {
    saving.value = false
  }
}

const jsonDrawerOpen = ref(false)
const jsonText = ref('')
const jsonApplying = ref(false)

function openJsonDrawer() {
  jsonText.value = JSON.stringify(buildDefinition(), null, 2)
  jsonDrawerOpen.value = true
}

async function applyJson() {
  let parsed
  try {
    parsed = JSON.parse(jsonText.value)
  } catch {
    message.error('JSON 格式错误，无法解析')
    return
  }
  jsonApplying.value = true
  try {
    const result = await workflowApi.validateDefinition(parsed)
    if (!result.valid) {
      message.error(`定义校验失败：${result.error}`)
      return
    }
    const flow = definitionToFlow(parsed)
    nodes.value = flow.nodes
    edges.value = flow.edges
    wfMeta.version = parsed.version || wfMeta.version
    selectedNodeId.value = null
    jsonDrawerOpen.value = false
    message.success('已应用 JSON 定义')
  } catch (err) {
    message.error(err.message || '校验请求失败')
  } finally {
    jsonApplying.value = false
  }
}

function goBack() {
  router.push('/workflows')
}
</script>

<style lang="less" scoped>
.workflow-editor-view {
  height: 100%;
  display: flex;
  flex-direction: column;
  background: var(--gray-0);
}

.editor-topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 16px;
  border-bottom: 1px solid var(--gray-150);
  flex-shrink: 0;

  .topbar-left {
    display: flex;
    align-items: center;
    gap: 4px;
    min-width: 0;
  }

  .wf-name-input {
    width: 240px;
    font-size: 15px;
    font-weight: 600;
    color: var(--color-text);
  }

  .validate-tag {
    margin-left: 4px;
  }

  .topbar-right {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-shrink: 0;
  }
}

.editor-body {
  flex: 1;
  display: flex;
  min-height: 0;
}

.node-palette {
  width: 168px;
  border-right: 1px solid var(--gray-150);
  padding: 12px;
  flex-shrink: 0;
  overflow-y: auto;

  .palette-title {
    font-size: 12px;
    font-weight: 600;
    color: var(--color-text-tertiary);
    margin-bottom: 8px;
  }

  .palette-item {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px;
    border: 1px solid var(--gray-150);
    border-radius: 8px;
    margin-bottom: 8px;
    cursor: grab;
    background: var(--gray-0);
    transition: border-color 0.2s;

    &:hover {
      border-color: var(--main-color);
    }

    &.disabled {
      opacity: 0.45;
      cursor: not-allowed;

      &:hover {
        border-color: var(--gray-150);
      }
    }

    .palette-icon {
      width: 28px;
      height: 28px;
      border-radius: 6px;
      display: flex;
      align-items: center;
      justify-content: center;
      flex-shrink: 0;
    }

    .palette-label {
      font-size: 13px;
      color: var(--color-text);
    }
  }

  .palette-tip {
    font-size: 12px;
    color: var(--color-text-tertiary);
    margin-top: 4px;
  }
}

.canvas-wrap {
  flex: 1;
  min-width: 0;
  background: var(--gray-50);
}

.config-panel {
  width: 360px;
  border-left: 1px solid var(--gray-150);
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
  background: var(--gray-0);

  .panel-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 12px 16px;
    border-bottom: 1px solid var(--gray-150);

    .panel-title {
      font-size: 14px;
      font-weight: 600;
      color: var(--color-text);
    }
  }

  .panel-body {
    flex: 1;
    overflow-y: auto;
    padding: 16px;
  }

  .field {
    margin-bottom: 16px;

    .field-label {
      font-size: 13px;
      color: var(--color-text-secondary);
      margin-bottom: 6px;

      &.required::after {
        content: ' *';
        color: var(--color-error-500);
      }
    }

    .field-tip {
      font-size: 12px;
      color: var(--color-text-tertiary);
      margin-top: 6px;
    }
  }

  .case-row {
    display: flex;
    align-items: center;
    gap: 6px;
    margin-bottom: 8px;

    .case-index {
      font-size: 12px;
      font-weight: 600;
      color: var(--color-warning-700);
      flex-shrink: 0;
      width: 24px;
    }

    .case-expr {
      font-family: monospace;
    }
  }

  .delete-node-btn {
    margin-top: 8px;
  }

  .advanced-collapse {
    margin-top: 4px;

    :deep(.ant-collapse-header) {
      padding: 6px 0;
      font-size: 13px;
      color: var(--color-gray-500);
    }

    :deep(.ant-collapse-content-box) {
      padding: 0 0 4px;
    }
  }
}

.json-textarea {
  font-family: monospace;
  font-size: 12px;
}

.drawer-footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 12px;
}
</style>
