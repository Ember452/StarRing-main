<template>
  <!-- 眼球容器：withWhite=false 时只渲染瞳孔（相当于 Pupil 模式） -->
  <div
    ref="eyeRef"
    class="eyeball"
    :class="{ 'no-white': !withWhite }"
    :style="{
      width: `${size}px`,
      height: isBlinking ? '2px' : `${size}px`,
      backgroundColor: withWhite ? eyeColor : 'transparent'
    }"
  >
    <div
      v-if="!isBlinking"
      class="pupil"
      :style="{
        width: `${pupilSize}px`,
        height: `${pupilSize}px`,
        backgroundColor: pupilColor,
        transform: `translate(${pupilPosition.x}px, ${pupilPosition.y}px)`
      }"
    ></div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'

const props = defineProps({
  size: { type: Number, default: 48 },
  pupilSize: { type: Number, default: 16 },
  maxDistance: { type: Number, default: 10 },
  eyeColor: { type: String, default: 'white' },
  pupilColor: { type: String, default: 'black' },
  isBlinking: { type: Boolean, default: false },
  // 强制注视方向；为 null 时跟随鼠标
  forceLookX: { type: Number, default: null },
  forceLookY: { type: Number, default: null },
  // 是否显示白色眼球背景；false 时只渲染瞳孔
  withWhite: { type: Boolean, default: true }
})

const eyeRef = ref(null)
const mouseX = ref(0)
const mouseY = ref(0)

const handleMouseMove = (e) => {
  mouseX.value = e.clientX
  mouseY.value = e.clientY
}

onMounted(() => {
  window.addEventListener('mousemove', handleMouseMove)
})

onUnmounted(() => {
  window.removeEventListener('mousemove', handleMouseMove)
})

// 瞳孔位置：有强制方向用强制方向，否则跟随鼠标
const pupilPosition = computed(() => {
  if (props.forceLookX !== null && props.forceLookY !== null) {
    return { x: props.forceLookX, y: props.forceLookY }
  }
  if (!eyeRef.value) return { x: 0, y: 0 }
  const rect = eyeRef.value.getBoundingClientRect()
  const centerX = rect.left + rect.width / 2
  const centerY = rect.top + rect.height / 2
  const deltaX = mouseX.value - centerX
  const deltaY = mouseY.value - centerY
  const distance = Math.min(Math.sqrt(deltaX ** 2 + deltaY ** 2), props.maxDistance)
  const angle = Math.atan2(deltaY, deltaX)
  return {
    x: Math.cos(angle) * distance,
    y: Math.sin(angle) * distance
  }
})
</script>

<style scoped>
.eyeball {
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
  transition: all 0.15s ease;
  position: relative;
}

.eyeball.no-white {
  background-color: transparent !important;
  overflow: visible;
}

.pupil {
  border-radius: 50%;
  transition: transform 0.1s ease-out;
}
</style>
