/**
 * 工作流节点类型元数据
 * 与后端 backend/package/StarRing/agents/buildin/workflow/definition.py 的节点契约对齐
 */
import { Play, Flag, Sparkles, GitBranch, AppWindow, Wrench, Database } from 'lucide-vue-next'

/**
 * 节点类型展示配置（色系遵循 design.md：start/end=灰、llm=主色、condition=警告、application-call/kb-retrieval=信息、tool=成功）
 * key 为「面板类型」：start / end 分开展示，序列化时均映射为后端 node_type='start-end'
 */
export const NODE_META = {
  start: {
    nodeType: 'start-end',
    kind: 'start',
    label: '开始',
    icon: Play,
    color: 'var(--gray-800)',
    bg: 'var(--gray-50)'
  },
  end: {
    nodeType: 'start-end',
    kind: 'end',
    label: '结束',
    icon: Flag,
    color: 'var(--gray-800)',
    bg: 'var(--gray-50)'
  },
  llm: {
    nodeType: 'llm',
    label: 'LLM',
    icon: Sparkles,
    color: 'var(--main-color)',
    bg: 'var(--main-30)'
  },
  condition: {
    nodeType: 'condition',
    label: '条件分支',
    icon: GitBranch,
    color: 'var(--color-warning-700)',
    bg: 'var(--color-warning-50)'
  },
  'application-call': {
    nodeType: 'application-call',
    label: '调用智能体',
    icon: AppWindow,
    color: 'var(--color-info-700)',
    bg: 'var(--color-info-50)'
  },
  tool: {
    nodeType: 'tool',
    label: '工具',
    icon: Wrench,
    color: 'var(--color-success-700)',
    bg: 'var(--color-success-50)'
  },
  'kb-retrieval': {
    nodeType: 'kb-retrieval',
    label: '知识检索',
    icon: Database,
    color: 'var(--color-info-700)',
    bg: 'var(--color-info-50)'
  }
}

/** 根据后端节点 node_type + config 反查面板类型 key */
export function metaKeyOf(nodeType, config = {}) {
  if (nodeType === 'start-end') return config.kind === 'end' ? 'end' : 'start'
  return nodeType
}

let seq = 0

/** 创建新节点（Vue Flow 格式），metaKey 为 NODE_META 的 key */
export function createFlowNode(metaKey, position) {
  const meta = NODE_META[metaKey]
  const id = `${metaKey}_${Date.now().toString(36)}_${seq++}`
  const config = {}
  if (meta.kind) config.kind = meta.kind
  if (metaKey === 'llm') {
    config.system_prompt = ''
    config.model = ''
    config.input_template = ''
    config.tools = []
    config.mcps = []
  } else if (metaKey === 'condition') {
    config.cases = [{ when: '', then: null }]
    config.default = null
  } else if (metaKey === 'application-call') {
    config.target_agent_slug = ''
    config.input_template = ''
  } else if (metaKey === 'tool') {
    config.tool_source = 'buildin'
    config.tool_name = ''
    config.mcp_server = ''
    config.args = {}
  } else if (metaKey === 'kb-retrieval') {
    config.query = ''
    config.kb_ids = []
    config.top_k = 5
  }
  return {
    id,
    type: meta.nodeType,
    position: { ...position },
    data: { name: meta.label, config }
  }
}
