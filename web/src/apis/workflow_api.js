import { apiGet, apiPost, apiPut, apiDelete } from './base'

/**
 * 工作流 API 模块
 * 包含工作流的 CRUD 与定义校验（对应 backend/server/routers/workflow_router.py）
 * 权限要求：任何已登录用户均可管理自己创建的工作流
 */

export const workflowApi = {
  /**
   * 创建工作流
   * @param {Object} data - 工作流数据
   * @param {string} data.name - 名称
   * @param {string} data.slug - 唯一 slug（冲突返回 409）
   * @param {string} [data.desc] - 描述
   * @param {Object} [data.definition] - 工作流定义 JSON（{nodes, edges, version}）
   * @param {boolean} [data.is_active=true] - 是否启用
   * @returns {Promise} - 创建后的工作流
   */
  create: (data) => apiPost('/api/workflows', data),

  /**
   * 列出当前用户的工作流
   * @param {Object} [params] - 过滤参数
   * @param {boolean} [params.is_active] - 按启用状态过滤
   * @param {number} [params.offset=0]
   * @param {number} [params.limit=50]
   * @returns {Promise<{workflows: Array}>}
   */
  list: (params = {}) => {
    const query = new URLSearchParams()
    if (params.is_active !== undefined) query.set('is_active', params.is_active)
    if (params.offset !== undefined) query.set('offset', params.offset)
    if (params.limit !== undefined) query.set('limit', params.limit)
    const qs = query.toString()
    return apiGet(qs ? `/api/workflows?${qs}` : '/api/workflows')
  },

  /**
   * 工作流详情
   * @param {string} workflowId
   * @returns {Promise} - 工作流对象（含 definition）
   */
  detail: (workflowId) => apiGet(`/api/workflows/${workflowId}`),

  /**
   * 更新工作流（仅更新传入字段；传 definition 时后端版本号自增）
   * @param {string} workflowId
   * @param {Object} fields - 待更新字段 {name?, desc?, definition?, is_active?}
   * @returns {Promise} - 更新后的工作流
   */
  update: (workflowId, fields) => apiPut(`/api/workflows/${workflowId}`, fields),

  /**
   * 删除工作流（物理删除）
   * @param {string} workflowId
   * @returns {Promise<{id: string, deleted: boolean}>}
   */
  remove: (workflowId) => apiDelete(`/api/workflows/${workflowId}`),

  /**
   * 校验工作流定义（不执行，不需要先保存），用于编辑器实时校验
   * @param {Object} definition - 工作流定义 JSON（{nodes, edges, version}）
   * @returns {Promise<{valid: boolean, node_count?, edge_count?, start_node_id?, end_node_id?, version?, error?}>}
   */
  validateDefinition: (definition) => apiPost('/api/workflows/validate', definition),

  /**
   * 获取编辑器的工具/MCP 选项（普通用户可用，供 tool 节点与 llm 节点挂工具）
   * @returns {Promise<{tools: Array<{key, name, description}>, mcps: Array<{key, name, description}>}>}
   */
  resourceOptions: () => apiGet('/api/workflows/resource-options'),
}
