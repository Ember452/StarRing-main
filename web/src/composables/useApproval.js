import { reactive } from 'vue'
import { normalizeQuestions } from '@/utils/questionUtils'

const APPROVAL_REQUIRED_STATUSES = new Set([
  'ask_user_question_required',
  'human_approval_required'
])

const extractQuestionPayload = (chunk) => {
  const interruptInfo = chunk?.interrupt_info || {}
  const rawQuestions = chunk?.questions || interruptInfo?.questions || []
  const source = chunk?.source || interruptInfo?.source || 'interrupt'
  const questions = normalizeQuestions(rawQuestions)

  return {
    questions,
    source
  }
}

/** 判断 chunk 是否为 human_review 审批中断 */
const isHumanReviewChunk = (chunk) =>
  chunk?.status === 'human_approval_required' && chunk?.interrupt_type === 'human_review'

const extractHumanReviewPayload = (chunk) => ({
  interruptType: 'human_review',
  message: chunk?.message || '请审核',
  nodeName: chunk?.node_name || '审核节点',
  nodeId: chunk?.node_id || '',
  source: chunk?.source || 'human_review',
})

export const extractPendingInterrupt = (chunk, threadId) => {
  // human_review 审批中断：不需要 questions，直接构建审批 payload
  if (isHumanReviewChunk(chunk)) {
    return {
      questions: [],
      interruptType: 'human_review',
      humanReview: extractHumanReviewPayload(chunk),
      source: chunk?.source || 'human_review',
      status: chunk?.status || '',
      threadId: chunk?.thread_id || threadId,
      parentRunId: chunk?.run_id || chunk?.parent_run_id || null
    }
  }

  const payload = extractQuestionPayload(chunk)
  if (!payload.questions.length) return null

  return {
    questions: payload.questions,
    source: payload.source,
    status: chunk?.status || '',
    threadId: chunk?.thread_id || threadId,
    parentRunId: chunk?.run_id || chunk?.parent_run_id || null
  }
}

export function useApproval({ getThreadState, fetchThreadMessages }) {
  const approvalState = reactive({
    showModal: false,
    questions: [],
    status: '',
    threadId: null,
    parentRunId: null,
    interruptType: '',
    humanReview: null
  })

  const applyInterruptToApprovalState = (pendingInterrupt, fallbackThreadId) => {
    approvalState.showModal = true
    approvalState.questions = pendingInterrupt.questions
    approvalState.status = pendingInterrupt.status || ''
    approvalState.threadId = pendingInterrupt.threadId || fallbackThreadId
    approvalState.parentRunId = pendingInterrupt.parentRunId || null
    approvalState.interruptType = pendingInterrupt.interruptType || ''
    approvalState.humanReview = pendingInterrupt.humanReview || null
  }

  const clearApprovalState = () => {
    approvalState.showModal = false
    approvalState.questions = []
    approvalState.status = ''
    approvalState.threadId = null
    approvalState.parentRunId = null
    approvalState.interruptType = ''
    approvalState.humanReview = null
  }

  const processApprovalInStream = (chunk, threadId, currentAgentId) => {
    if (!APPROVAL_REQUIRED_STATUSES.has(chunk.status)) {
      return false
    }

    const threadState = getThreadState(threadId)
    if (!threadState) return false

    const pendingInterrupt = extractPendingInterrupt(chunk, threadId)
    if (!pendingInterrupt) return false

    threadState.isStreaming = false
    threadState.pendingInterrupt = pendingInterrupt

    applyInterruptToApprovalState(pendingInterrupt, threadId)

    fetchThreadMessages({ agentId: currentAgentId, threadId })

    return true
  }

  const restoreInterruptFromThreadState = (threadId) => {
    const threadState = getThreadState(threadId)
    const pendingInterrupt = threadState?.pendingInterrupt
    if (!pendingInterrupt) return false
    // human_review：有 interruptType 无 questions 亦可恢复
    if (!pendingInterrupt.interruptType && !pendingInterrupt.questions?.length) return false

    threadState.isStreaming = false
    threadState.replyLoadingVisible = false
    threadState.pendingRequestId = null
    applyInterruptToApprovalState(pendingInterrupt, threadId)
    return true
  }

  const hideApprovalState = () => {
    clearApprovalState()
  }

  const resetApprovalState = () => {
    const threadState = getThreadState(approvalState.threadId)
    if (threadState) {
      threadState.pendingInterrupt = null
    }
    clearApprovalState()
  }

  return {
    approvalState,
    processApprovalInStream,
    restoreInterruptFromThreadState,
    hideApprovalState,
    resetApprovalState
  }
}
