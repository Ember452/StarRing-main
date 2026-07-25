/**
 * 工作流定义 <-> Vue Flow 画布数据 双向转换
 *
 * 后端契约（definition.py）：
 *   node: {id, node_type, name, config, position?}   position 为前端附加字段，后端忽略但随原始 dict 持久化
 *   edge: {source, target, branch?}                   branch 仅 condition 出边使用，等于 config.cases[i].then / default 的目标节点 id
 *   definition: {nodes, edges, version, viewport?}
 *
 * condition 分支同步约定：
 *   - case 出边的 sourceHandle = `case-${index}`，连线时 config.cases[index].then = 目标节点 id
 *   - default 出边的 sourceHandle = 'default'，连线时 config.default = 目标节点 id
 */

/** 后端 definition JSON -> Vue Flow { nodes, edges } */
export function definitionToFlow(definition) {
  const defNodes = definition?.nodes || []
  const defEdges = definition?.edges || []

  const nodes = defNodes.map((n, i) => ({
    id: n.id,
    type: n.node_type,
    position: n.position || { x: 120, y: 80 + i * 120 },
    data: {
      name: n.name || '',
      config: JSON.parse(JSON.stringify(n.config || {})),
    },
  }))

  const nodeMap = new Map(defNodes.map((n) => [n.id, n]))
  const edges = defEdges.map((e, i) => {
    const flowEdge = {
      id: `e_${e.source}_${e.target}_${i}`,
      source: e.source,
      target: e.target,
    }
    // condition 出边：根据 cases[i].then / default 反推 sourceHandle
    const sourceNode = nodeMap.get(e.source)
    if (sourceNode?.node_type === 'condition') {
      const cases = sourceNode.config?.cases || []
      const caseIdx = cases.findIndex((c) => c.then === e.target)
      flowEdge.sourceHandle = caseIdx >= 0 ? `case-${caseIdx}` : 'default'
    }
    return flowEdge
  })

  return { nodes, edges }
}

/** Vue Flow { nodes, edges } -> 后端 definition JSON（含 position/viewport 附加字段） */
export function flowToDefinition(nodes, edges, { version = 1, viewport = null } = {}) {
  const defNodes = nodes.map((n) => {
    const config = JSON.parse(JSON.stringify(n.data.config || {}))
    // condition 节点：根据出边的 sourceHandle 同步 cases[i].then 与 default
    if (n.type === 'condition') {
      const cases = config.cases || []
      cases.forEach((c) => { c.then = null })
      config.default = null
      for (const e of edges) {
        if (e.source !== n.id) continue
        if (e.sourceHandle === 'default') {
          config.default = e.target
        } else if (e.sourceHandle?.startsWith('case-')) {
          const idx = Number(e.sourceHandle.slice(5))
          if (cases[idx]) cases[idx].then = e.target
        }
      }
    }
    return {
      id: n.id,
      node_type: n.type,
      name: n.data.name || '',
      config,
      position: { x: Math.round(n.position.x), y: Math.round(n.position.y) },
    }
  })

  const defEdges = edges.map((e) => {
    const edge = { source: e.source, target: e.target }
    // condition 出边写 branch = 目标节点 id（对应 cases[i].then / default）
    const sourceNode = nodes.find((n) => n.id === e.source)
    if (sourceNode?.type === 'condition') edge.branch = e.target
    return edge
  })

  const definition = { nodes: defNodes, edges: defEdges, version }
  if (viewport) definition.viewport = viewport
  return definition
}
