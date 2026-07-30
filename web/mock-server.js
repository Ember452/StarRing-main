/**
 * Vite Mock Server 插件
 *
 * 用途：在后端未启动时，mock 关键 API 让前端界面可正常浏览
 * 启用方式：MOCK_MODE=true pnpm dev
 * 关闭方式：直接 pnpm dev（默认走真实后端代理）
 *
 * 只 mock 首页健康检查、品牌信息、登录认证、Agent 列表等关键路径，
 * 其余 /api/ 请求兜底返回空数据，保证页面不崩溃。
 */

/** Mock 用户信息（与 seed_initial_users.py 保持一致） */
const MOCK_USER = {
  access_token: 'mock-jwt-token-solis',
  token_type: 'bearer',
  user_id: 1,
  id: 1,
  username: 'solis',
  uid: 'solis',
  phone_number: '',
  avatar: '',
  role: 'superadmin',
  department_id: null,
  department_name: ''
}

/** Mock 默认 Agent */
const MOCK_AGENT = {
  agent_id: 'default-chatbot',
  name: '默认助手',
  slug: 'default-chatbot',
  is_builtin: true,
  is_subagent: false,
  description: '内置默认聊天助手',
  config_json: {},
  configurable_items: {}
}

/** Mock 知识库列表（用于 /knowledge 一级导航展示） */
const MOCK_DATABASES = [
  {
    kb_id: 'mock-kb-001',
    name: '产品使用手册',
    kb_type: 'milvus',
    description: 'StarRing 平台产品使用文档与最佳实践合集',
    created_by: 'solis',
    created_at: '2026-07-15T08:30:00Z',
    share_config: { access_level: 'private', department_ids: [], user_uids: [] },
    row_count: 12,
    is_owner: true
  },
  {
    kb_id: 'mock-kb-002',
    name: '公司制度库',
    kb_type: 'milvus',
    description: '全员共享的公司制度与流程文档',
    created_by: 'admin',
    created_at: '2026-06-01T10:00:00Z',
    share_config: { access_level: 'global', department_ids: [], user_uids: [] },
    row_count: 156,
    is_owner: false
  },
  {
    kb_id: 'mock-kb-003',
    name: '技术文档库',
    kb_type: 'milvus',
    description: '研发部门共享技术文档与代码规范',
    created_by: 'admin',
    created_at: '2026-05-20T09:15:00Z',
    share_config: { access_level: 'department', department_ids: [1], user_uids: [] },
    row_count: 89,
    is_owner: false
  }
]

/** 根据方法和路径返回 mock 响应，未匹配返回 null */
function getMockResponse(method, path) {
  // --- 健康检查 ---
  if (method === 'GET' && path === '/api/system/health') {
    return { status: 'ok', version: 'mock-1.0' }
  }

  // --- 系统品牌信息 ---
  if (method === 'GET' && path === '/api/system/info') {
    return {
      success: true,
      data: {
        organization: { name: 'StarRing', logo: '', avatar: '' },
        branding: {
          name: 'StarRing',
          title: 'StarRing 星环智库',
          subtitle: '融合 RAG 与知识图谱的智能体开发平台',
          subtitles: [
            '融合 RAG 与知识图谱',
            '基于 LangGraph 构建',
            '支持多模型接入'
          ]
        },
        footer: {
          copyright: '© 2026 StarRing',
          user_agreement_url: '',
          privacy_policy_url: ''
        }
      }
    }
  }

  // --- 系统配置（模型供应商等） ---
  if (method === 'GET' && path === '/api/system/config') {
    return {
      default_model: 'siliconflow-cn:Qwen/Qwen2.5-72B-Instruct',
      fast_model: 'siliconflow-cn:Qwen/Qwen2.5-7B-Instruct',
      embed_model: 'siliconflow-cn:BAAI/bge-m3',
      reranker_model: 'siliconflow-cn:BAAI/bge-reranker-v2-m3'
    }
  }

  // --- 认证：登录 ---
  if (method === 'POST' && path === '/api/auth/token') {
    return MOCK_USER
  }

  // --- 认证：当前用户 ---
  if (method === 'GET' && path === '/api/auth/me') {
    return MOCK_USER
  }

  // --- 认证：首次运行检查 ---
  if (method === 'GET' && path === '/api/auth/check-first-run') {
    return { first_run: false }
  }

  // --- Agent 列表 ---
  if (method === 'GET' && path === '/api/agent') {
    return { agents: [MOCK_AGENT] }
  }

  // --- Agent backends ---
  if (method === 'GET' && path === '/api/agent/backends') {
    return { backends: ['langgraph'] }
  }

  // --- Agent 详情 ---
  if (method === 'GET' && path.startsWith('/api/agent/')) {
    return MOCK_AGENT
  }

  // --- 知识库列表（管理员全量） ---
  if (method === 'GET' && path === '/api/knowledge/databases') {
    return { databases: MOCK_DATABASES }
  }

  // --- 知识库列表（普通用户可访问） ---
  if (method === 'GET' && path === '/api/knowledge/databases/accessible') {
    return { databases: MOCK_DATABASES }
  }

  // --- 知识库类型 ---
  if (method === 'GET' && path === '/api/knowledge/types') {
    return {
      kb_types: {
        milvus: {
          name: 'milvus',
          description: '基于 Milvus 向量数据库的知识库，支持文档检索与图谱构建',
          requires_embedding_model: true,
          supports_documents: true,
          create_params: { options: [] }
        }
      }
    }
  }

  // --- 知识库详情 ---
  if (method === 'GET' && /^\/api\/knowledge\/databases\/[^/]+$/.test(path)) {
    const kbId = path.split('/').pop()
    const db = MOCK_DATABASES.find((d) => d.kb_id === kbId) || MOCK_DATABASES[0]
    return { ...db, files: {} }
  }

  // --- 知识库统计 ---
  if (method === 'GET' && path === '/api/knowledge/stats') {
    return { total_databases: MOCK_DATABASES.length, total_documents: 257 }
  }

  // --- MCP 服务器列表 ---
  if (method === 'GET' && path === '/api/system/mcp-servers') {
    return { data: [] }
  }

  // --- Skill 列表 ---
  if (method === 'GET' && path === '/api/skills/accessible') {
    return { data: [] }
  }

  // --- Dashboard 统计 ---
  if (method === 'GET' && path.startsWith('/api/dashboard')) {
    return {
      total_users: 1,
      total_agents: 1,
      total_knowledge_bases: 0,
      total_documents: 0,
      total_conversations: 0
    }
  }

  // --- 模型供应商 ---
  if (method === 'GET' && path === '/api/system/providers') {
    return {
      success: true,
      data: [
        { provider_id: 'siliconflow-cn', name: '硅基流动', enabled: true }
      ]
    }
  }

  return null
}

/** Vite 插件：拦截 /api/ 请求返回 mock 数据 */
export function mockServerPlugin() {
  return {
    name: 'vite-plugin-mock-server',
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        if (!req.url || !req.url.startsWith('/api/')) {
          return next()
        }

        const url = new URL(req.url, 'http://localhost')
        const path = url.pathname
        const method = req.method

        const mockBody = getMockResponse(method, path)

        res.setHeader('Content-Type', 'application/json')

        if (mockBody !== null) {
          res.statusCode = 200
          res.end(JSON.stringify(mockBody))
          return
        }

        // 兜底：未匹配的 /api/ 请求返回空数据，避免前端崩溃
        res.statusCode = 200
        res.end(JSON.stringify({ success: true, data: [] }))
      })
    }
  }
}
