import { apiGet, apiDelete } from './base'

/**
 * 长期记忆 API 模块
 * 记忆仅本人可见/可删：列表、单条删除、清空全部
 * 权限要求：任何已登录用户均可管理自己的记忆
 */

export const memoryApi = {
  /**
   * 列出当前用户全部记忆（按创建时间倒序）
   * @returns {Promise<{memories: Array, total: number}>}
   */
  list: () => apiGet('/api/memory'),

  /**
   * 删除一条记忆
   * @param {string} memoryId
   * @returns {Promise<{message: string, id: string}>}
   */
  remove: (memoryId) => apiDelete(`/api/memory/${memoryId}`),

  /**
   * 清空本人全部记忆
   * @returns {Promise<{message: string, deleted: number}>}
   */
  clear: () => apiDelete('/api/memory'),
}
