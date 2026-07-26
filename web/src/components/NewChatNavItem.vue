<template>
  <!-- 展开态：创建新对话 + 行尾 History 触发器 -->
  <div v-if="!collapsed" class="new-chat-nav-item">
    <RouterLink
      to="/agent"
      class="nav-item new-chat-link"
      :class="{ active: isExactActive }"
      active-class=""
      @click.stop
    >
      <MessageCirclePlus class="icon" :size="18" />
      <span class="nav-text">创建新对话</span>
      <a-popover
        trigger="hover"
        placement="rightBottom"
        :mouseEnterDelay="0.15"
        :mouseLeaveDelay="0.3"
        overlay-class-name="history-popover"
        :destroy-on-hide="true"
      >
        <template #content>
          <div class="history-popover-content">
            <ConversationNavSection
              :current-chat-id="currentChatId"
              :chats-list="chatsList"
              :has-more-chats="hasMoreChats"
              :is-loading-more="isLoadingMore"
              @select-chat="(id) => emit('select-chat', id)"
              @delete-chat="(id) => emit('delete-chat', id)"
              @rename-chat="(payload) => emit('rename-chat', payload)"
              @toggle-pin="(id) => emit('toggle-pin', id)"
              @load-more-chats="() => emit('load-more-chats')"
            />
          </div>
        </template>
        <button
          type="button"
          class="history-trigger"
          aria-label="查看最近对话"
          @click.stop
        >
          <History :size="16" />
        </button>
      </a-popover>
    </RouterLink>
  </div>

  <!-- 折叠态：创建新对话 + 独立 History 行 -->
  <template v-else>
    <RouterLink
      to="/agent"
      class="nav-item new-chat-link-collapsed"
      :class="{ active: isExactActive }"
      active-class=""
      @click.stop
    >
      <a-tooltip placement="right" :open="undefined">
        <template #title>创建新对话</template>
        <MessageCirclePlus class="icon" :size="18" />
      </a-tooltip>
    </RouterLink>
    <a-popover
      trigger="hover"
      placement="right"
      :mouseEnterDelay="0.15"
      :mouseLeaveDelay="0.3"
      overlay-class-name="history-popover"
      :destroy-on-hide="true"
    >
      <template #content>
        <div class="history-popover-content">
          <ConversationNavSection
            :current-chat-id="currentChatId"
            :chats-list="chatsList"
            :has-more-chats="hasMoreChats"
            :is-loading-more="isLoadingMore"
            @select-chat="(id) => emit('select-chat', id)"
            @delete-chat="(id) => emit('delete-chat', id)"
            @rename-chat="(payload) => emit('rename-chat', payload)"
            @toggle-pin="(id) => emit('toggle-pin', id)"
            @load-more-chats="() => emit('load-more-chats')"
          />
        </div>
      </template>
      <button
        type="button"
        class="nav-item history-trigger-collapsed"
        aria-label="查看最近对话"
        @click.stop
      >
        <a-tooltip placement="right" :open="undefined">
          <template #title>最近对话</template>
          <History class="icon" :size="18" />
        </a-tooltip>
      </button>
    </a-popover>
  </template>
</template>

<script setup>
import { computed } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import { MessageCirclePlus, History } from 'lucide-vue-next'
import ConversationNavSection from '@/components/ConversationNavSection.vue'

defineProps({
  currentChatId: {
    type: String,
    default: null
  },
  chatsList: {
    type: Array,
    default: () => []
  },
  hasMoreChats: {
    type: Boolean,
    default: false
  },
  isLoadingMore: {
    type: Boolean,
    default: false
  },
  collapsed: {
    type: Boolean,
    default: false
  }
})

const emit = defineEmits([
  'select-chat',
  'delete-chat',
  'rename-chat',
  'toggle-pin',
  'load-more-chats'
])

const route = useRoute()

// 创建新对话项仅在 /agent 路径精确匹配时高亮（不匹配子路径）
const isExactActive = computed(() => route.path === '/agent')
</script>

<style lang="less" scoped>
.new-chat-nav-item {
  display: flex;
  width: 100%;
}

.new-chat-link {
  display: flex;
  align-items: center;
  width: 100%;
  height: 36px;
  padding: 0 10px;
  border: 1px solid transparent;
  border-radius: 8px;
  background-color: transparent;
  color: var(--gray-700);
  font-size: 14px;
  font-weight: 450;
  text-decoration: none;
  cursor: pointer;
  transition:
    background-color 0.2s ease-in-out,
    border-color 0.2s ease-in-out,
    color 0.2s ease-in-out;

  .icon {
    flex: 0 0 16px;
    width: 16px;
    height: 16px;
  }

  .nav-text {
    flex: 1 1 auto;
    min-width: 0;
    margin-left: 8px;
    overflow: hidden;
    line-height: 20px;
    font-weight: 450;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  &:hover {
    border-color: transparent;
    background-color: var(--main-20);
    color: var(--main-color);
  }

  &.active {
    border-color: transparent;
    background-color: color-mix(in srgb, var(--main-color) 6%, var(--gray-0));
    font-weight: 600;
    color: var(--main-color);
  }
}

.history-trigger {
  display: inline-flex;
  flex: 0 0 24px;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  padding: 0;
  margin-left: auto;
  border: 1px solid transparent;
  border-radius: 6px;
  background: transparent;
  color: var(--gray-600);
  cursor: pointer;
  transition:
    background-color 0.18s ease,
    color 0.18s ease;

  &:hover,
  &:focus-visible {
    background: var(--main-20);
    color: var(--main-color);
    outline: none;
  }
}

.new-chat-link-collapsed {
  display: flex;
  align-items: center;
  justify-content: flex-start;
  width: 36px;
  height: 36px;
  padding: 0 10px;
  border: 1px solid transparent;
  border-radius: 8px;
  background-color: transparent;
  color: var(--gray-700);
  text-decoration: none;
  cursor: pointer;
  transition:
    background-color 0.2s ease-in-out,
    color 0.2s ease-in-out;

  &:hover {
    background-color: var(--main-20);
    color: var(--main-color);
  }

  &.active {
    background-color: color-mix(in srgb, var(--main-color) 6%, var(--gray-0));
    font-weight: 600;
    color: var(--main-color);
  }
}

.history-trigger-collapsed {
  display: flex;
  align-items: center;
  justify-content: flex-start;
  width: 36px;
  height: 36px;
  padding: 0 10px;
  border: 1px solid transparent;
  border-radius: 8px;
  background-color: transparent;
  color: var(--gray-700);
  cursor: pointer;
  transition:
    background-color 0.2s ease-in-out,
    color 0.2s ease-in-out;

  &:hover {
    background-color: var(--main-20);
    color: var(--main-color);
  }
}
</style>

<style lang="less">
// Popover 浮层全局样式（非 scoped，因为浮层渲染在 body 下）
.history-popover {
  .ant-popover-inner {
    width: 280px;
    max-height: min(60vh, 480px);
    padding: 8px;
    background-color: var(--gray-0);
    border: 1px solid var(--gray-100);
    border-radius: 8px;
    box-shadow: var(--shadow-2);
    overflow: hidden;
    transition:
      opacity 0.18s ease,
      transform 0.18s ease;
  }

  .ant-popover-inner-content {
    padding: 0;
    max-height: min(60vh, 480px);
    overflow: hidden;
  }

  .history-popover-content {
    display: flex;
    flex-direction: column;
    max-height: min(60vh, 480px);
  }
}
</style>
