import { apiGet, apiPost, apiPatch, apiDelete } from './base'

/**
 * 触发器 API 模块
 * 包含 cron / webhook 触发器的 CRUD、secret 轮换、执行历史、webhook invoke 等
 * 权限要求：任何已登录用户均可管理自己创建的触发器
 */

export const triggerApi = {
  /**
   * 创建触发器
   * @param {Object} data - 触发器数据
   * @param {string} data.name - 名称
   * @param {string} data.trigger_type - 类型: 'cron' | 'webhook'
   * @param {string} data.agent_id - 关联的 Agent slug
   * @param {Object} data.config - 配置（cron: {cron_expr, timezone}; webhook: 自动生成 secret）
   * @param {string} [data.desc] - 描述
   * @param {string} [data.input_query] - 触发器执行时的输入 query
   * @param {boolean} [data.is_active=true] - 是否启用
   * @returns {Promise} - 创建后的触发器（含完整 secret）
   */
  create: (data) => apiPost('/api/triggers', data),

  /**
   * 列出当前用户的触发器
   * @param {Object} [params] - 过滤参数
   * @param {string} [params.trigger_type] - 按类型过滤
   * @param {string} [params.agent_id] - 按 Agent 过滤
   * @param {boolean} [params.is_active] - 按启用状态过滤
   * @param {number} [params.offset=0]
   * @param {number} [params.limit=50]
   * @returns {Promise<{triggers: Array}>}
   */
  list: (params = {}) => {
    const query = new URLSearchParams()
    if (params.trigger_type) query.set('trigger_type', params.trigger_type)
    if (params.agent_id) query.set('agent_id', params.agent_id)
    if (params.is_active !== undefined) query.set('is_active', params.is_active)
    if (params.offset !== undefined) query.set('offset', params.offset)
    if (params.limit !== undefined) query.set('limit', params.limit)
    const qs = query.toString()
    return apiGet(qs ? `/api/triggers?${qs}` : '/api/triggers')
  },

  /**
   * 触发器详情（含完整 secret，仅创建者可见）
   * @param {string} triggerId
   * @returns {Promise<{trigger: Object}>}
   */
  detail: (triggerId) => apiGet(`/api/triggers/${triggerId}`),

  /**
   * 更新触发器字段
   * @param {string} triggerId
   * @param {Object} fields - 待更新字段
   * @returns {Promise<{trigger: Object}>}
   */
  update: (triggerId, fields) => apiPatch(`/api/triggers/${triggerId}`, fields),

  /**
   * 删除触发器
   * @param {string} triggerId
   * @returns {Promise<{success: boolean}>}
   */
  remove: (triggerId) => apiDelete(`/api/triggers/${triggerId}`),

  /**
   * 重新生成 webhook 触发器的 secret（仅 webhook 类型）
   * @param {string} triggerId
   * @returns {Promise<{trigger: Object}>} - 含新 secret
   */
  rotateSecret: (triggerId) => apiPost(`/api/triggers/${triggerId}/rotate-secret`),

  /**
   * 查询触发器执行历史
   * @param {string} triggerId
   * @param {Object} [params]
   * @param {number} [params.offset=0]
   * @param {number} [params.limit=20]
   * @returns {Promise<{runs: Array}>}
   */
  runs: (triggerId, params = {}) => {
    const query = new URLSearchParams()
    if (params.offset !== undefined) query.set('offset', params.offset)
    if (params.limit !== undefined) query.set('limit', params.limit)
    const qs = query.toString()
    return apiGet(qs ? `/api/triggers/${triggerId}/runs?${qs}` : `/api/triggers/${triggerId}/runs`)
  },
}
