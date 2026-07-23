<template>
  <!-- 4 个几何角色：紫(后) / 黑(中) / 橙(前左) / 黄(前右)，眼睛跟随鼠标，输入密码时偷看或捂眼 -->
  <div class="animated-characters">
    <!-- 紫色高矩形角色 - 最后层 -->
    <div ref="purpleRef" class="character character-purple" :style="purpleStyle">
      <div class="eyes eyes-purple" :style="purpleEyesStyle">
        <EyeBall
          :size="18"
          :pupil-size="7"
          :max-distance="5"
          eye-color="white"
          pupil-color="#2D2D2D"
          :is-blinking="isPurpleBlinking || isHidingPassword"
          :force-look-x="purpleForceLook.x"
          :force-look-y="purpleForceLook.y"
        />
        <EyeBall
          :size="18"
          :pupil-size="7"
          :max-distance="5"
          eye-color="white"
          pupil-color="#2D2D2D"
          :is-blinking="isPurpleBlinking || isHidingPassword"
          :force-look-x="purpleForceLook.x"
          :force-look-y="purpleForceLook.y"
        />
      </div>
    </div>

    <!-- 黑色高矩形角色 - 中间层 -->
    <div ref="blackRef" class="character character-black" :style="blackStyle">
      <div class="eyes eyes-black" :style="blackEyesStyle">
        <EyeBall
          :size="16"
          :pupil-size="6"
          :max-distance="4"
          eye-color="white"
          pupil-color="#2D2D2D"
          :is-blinking="isBlackBlinking || isHidingPassword"
          :force-look-x="blackForceLook.x"
          :force-look-y="blackForceLook.y"
        />
        <EyeBall
          :size="16"
          :pupil-size="6"
          :max-distance="4"
          eye-color="white"
          pupil-color="#2D2D2D"
          :is-blinking="isBlackBlinking || isHidingPassword"
          :force-look-x="blackForceLook.x"
          :force-look-y="blackForceLook.y"
        />
      </div>
    </div>

    <!-- 橙色半圆角色 - 前左层 -->
    <div ref="orangeRef" class="character character-orange" :style="orangeStyle">
      <div class="eyes eyes-orange" :style="orangeEyesStyle">
        <EyeBall
          :with-white="false"
          :size="12"
          :max-distance="5"
          pupil-color="#2D2D2D"
          :is-blinking="isHidingPassword"
          :force-look-x="orangeForceLook.x"
          :force-look-y="orangeForceLook.y"
        />
        <EyeBall
          :with-white="false"
          :size="12"
          :max-distance="5"
          pupil-color="#2D2D2D"
          :is-blinking="isHidingPassword"
          :force-look-x="orangeForceLook.x"
          :force-look-y="orangeForceLook.y"
        />
      </div>
    </div>

    <!-- 黄色高矩形角色 - 前右层 -->
    <div ref="yellowRef" class="character character-yellow" :style="yellowStyle">
      <div class="eyes eyes-yellow" :style="yellowEyesStyle">
        <EyeBall
          :with-white="false"
          :size="12"
          :max-distance="5"
          pupil-color="#2D2D2D"
          :is-blinking="isHidingPassword"
          :force-look-x="yellowForceLook.x"
          :force-look-y="yellowForceLook.y"
        />
        <EyeBall
          :with-white="false"
          :size="12"
          :max-distance="5"
          pupil-color="#2D2D2D"
          :is-blinking="isHidingPassword"
          :force-look-x="yellowForceLook.x"
          :force-look-y="yellowForceLook.y"
        />
      </div>
      <div class="mouth" :style="yellowMouthStyle"></div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import EyeBall from './EyeBall.vue'

const props = defineProps({
  isTyping: { type: Boolean, default: false },
  showPassword: { type: Boolean, default: false },
  passwordLength: { type: Number, default: 0 }
})

const mouseX = ref(0)
const mouseY = ref(0)
const isPurpleBlinking = ref(false)
const isBlackBlinking = ref(false)
const isLookingAtEachOther = ref(false)
const isPurplePeeking = ref(false)

const purpleRef = ref(null)
const blackRef = ref(null)
const yellowRef = ref(null)
const orangeRef = ref(null)

let purpleBlinkTimer = null
let blackBlinkTimer = null
let lookTimer = null
let peekTimer = null

const handleMouseMove = (e) => {
  mouseX.value = e.clientX
  mouseY.value = e.clientY
}

onMounted(() => {
  window.addEventListener('mousemove', handleMouseMove)
  // 紫色角色随机眨眼
  const schedulePurpleBlink = () => {
    purpleBlinkTimer = setTimeout(() => {
      isPurpleBlinking.value = true
      setTimeout(() => {
        isPurpleBlinking.value = false
        schedulePurpleBlink()
      }, 150)
    }, Math.random() * 4000 + 3000)
  }
  schedulePurpleBlink()
  // 黑色角色随机眨眼
  const scheduleBlackBlink = () => {
    blackBlinkTimer = setTimeout(() => {
      isBlackBlinking.value = true
      setTimeout(() => {
        isBlackBlinking.value = false
        scheduleBlackBlink()
      }, 150)
    }, Math.random() * 4000 + 3000)
  }
  scheduleBlackBlink()
})

onUnmounted(() => {
  window.removeEventListener('mousemove', handleMouseMove)
  clearTimeout(purpleBlinkTimer)
  clearTimeout(blackBlinkTimer)
  clearTimeout(lookTimer)
  clearTimeout(peekTimer)
})

// 输入时角色互相看
watch(
  () => props.isTyping,
  (val) => {
    if (val) {
      isLookingAtEachOther.value = true
      lookTimer = setTimeout(() => {
        isLookingAtEachOther.value = false
      }, 800)
    } else {
      isLookingAtEachOther.value = false
    }
  }
)

// 显示密码时紫色角色偷看
watch(
  [() => props.passwordLength, () => props.showPassword],
  () => {
    if (props.passwordLength > 0 && props.showPassword) {
      const schedulePeek = () => {
        peekTimer = setTimeout(() => {
          isPurplePeeking.value = true
          setTimeout(() => {
            isPurplePeeking.value = false
          }, 800)
        }, Math.random() * 3000 + 2000)
      }
      schedulePeek()
    } else {
      isPurplePeeking.value = false
    }
  }
)

// 根据鼠标位置计算角色面部偏移与身体倾斜
const calculatePosition = (el) => {
  if (!el) return { faceX: 0, faceY: 0, bodySkew: 0 }
  const rect = el.getBoundingClientRect()
  const centerX = rect.left + rect.width / 2
  const centerY = rect.top + rect.height / 3
  const deltaX = mouseX.value - centerX
  const deltaY = mouseY.value - centerY
  const faceX = Math.max(-15, Math.min(15, deltaX / 20))
  const faceY = Math.max(-10, Math.min(10, deltaY / 30))
  const bodySkew = Math.max(-6, Math.min(6, -deltaX / 120))
  return { faceX, faceY, bodySkew }
}

const isHidingPassword = computed(() => props.passwordLength > 0 && !props.showPassword)
const isShowingPassword = computed(() => props.passwordLength > 0 && props.showPassword)

const purplePos = computed(() => calculatePosition(purpleRef.value))
const blackPos = computed(() => calculatePosition(blackRef.value))
const orangePos = computed(() => calculatePosition(orangeRef.value))
const yellowPos = computed(() => calculatePosition(yellowRef.value))

const purpleStyle = computed(() => ({
  height: props.isTyping || isHidingPassword.value ? '440px' : '400px',
  transform: isShowingPassword.value
    ? 'skewX(0deg)'
    : props.isTyping || isHidingPassword.value
      ? `skewX(${(purplePos.value.bodySkew || 0) - 12}deg) translateX(40px)`
      : `skewX(${purplePos.value.bodySkew || 0}deg)`
}))

const purpleEyesStyle = computed(() => ({
  left: isShowingPassword.value
    ? '20px'
    : isLookingAtEachOther.value
      ? '55px'
      : `${45 + purplePos.value.faceX}px`,
  top: isShowingPassword.value
    ? '35px'
    : isLookingAtEachOther.value
      ? '65px'
      : `${40 + purplePos.value.faceY}px`
}))

const purpleForceLook = computed(() => {
  if (isShowingPassword.value) {
    return { x: isPurplePeeking.value ? 4 : -4, y: isPurplePeeking.value ? 5 : -4 }
  }
  if (isLookingAtEachOther.value) return { x: 3, y: 4 }
  return { x: null, y: null }
})

const blackStyle = computed(() => ({
  transform: isShowingPassword.value
    ? 'skewX(0deg)'
    : isLookingAtEachOther.value
      ? `skewX(${(blackPos.value.bodySkew || 0) * 1.5 + 10}deg) translateX(20px)`
      : props.isTyping || isHidingPassword.value
        ? `skewX(${(blackPos.value.bodySkew || 0) * 1.5}deg)`
        : `skewX(${blackPos.value.bodySkew || 0}deg)`
}))

const blackEyesStyle = computed(() => ({
  left: isShowingPassword.value
    ? '10px'
    : isLookingAtEachOther.value
      ? '32px'
      : `${26 + blackPos.value.faceX}px`,
  top: isShowingPassword.value
    ? '28px'
    : isLookingAtEachOther.value
      ? '12px'
      : `${32 + blackPos.value.faceY}px`
}))

const blackForceLook = computed(() => {
  if (isShowingPassword.value) return { x: -4, y: -4 }
  if (isLookingAtEachOther.value) return { x: 0, y: -4 }
  return { x: null, y: null }
})

const orangeStyle = computed(() => ({
  transform: isShowingPassword.value
    ? 'skewX(0deg)'
    : `skewX(${orangePos.value.bodySkew || 0}deg)`
}))

const orangeEyesStyle = computed(() => ({
  left: isShowingPassword.value ? '50px' : `${82 + (orangePos.value.faceX || 0)}px`,
  top: isShowingPassword.value ? '85px' : `${90 + (orangePos.value.faceY || 0)}px`
}))

const orangeForceLook = computed(() => {
  if (isShowingPassword.value) return { x: -5, y: -4 }
  return { x: null, y: null }
})

const yellowStyle = computed(() => ({
  transform: isShowingPassword.value
    ? 'skewX(0deg)'
    : `skewX(${yellowPos.value.bodySkew || 0}deg)`
}))

const yellowEyesStyle = computed(() => ({
  left: isShowingPassword.value ? '20px' : `${52 + (yellowPos.value.faceX || 0)}px`,
  top: isShowingPassword.value ? '35px' : `${40 + (yellowPos.value.faceY || 0)}px`
}))

const yellowForceLook = computed(() => {
  if (isShowingPassword.value) return { x: -5, y: -4 }
  return { x: null, y: null }
})

const yellowMouthStyle = computed(() => ({
  left: isShowingPassword.value ? '10px' : `${40 + (yellowPos.value.faceX || 0)}px`,
  top: isShowingPassword.value ? '88px' : `${88 + (yellowPos.value.faceY || 0)}px`
}))
</script>

<style scoped>
.animated-characters {
  position: relative;
  width: 550px;
  height: 400px;
}

.character {
  position: absolute;
  bottom: 0;
  transition: all 0.7s ease-in-out;
  transform-origin: bottom center;
}

.character-purple {
  left: 70px;
  width: 180px;
  height: 400px;
  background-color: #6c3ff5;
  border-radius: 10px 10px 0 0;
  z-index: 1;
}

.character-black {
  left: 240px;
  width: 120px;
  height: 310px;
  background-color: #2d2d2d;
  border-radius: 8px 8px 0 0;
  z-index: 2;
}

.character-orange {
  left: 0;
  width: 240px;
  height: 200px;
  background-color: #ff9b6b;
  border-radius: 120px 120px 0 0;
  z-index: 3;
}

.character-yellow {
  left: 310px;
  width: 140px;
  height: 230px;
  background-color: #e8d754;
  border-radius: 70px 70px 0 0;
  z-index: 4;
}

.eyes {
  position: absolute;
  display: flex;
  transition: all 0.7s ease-in-out;
}

.eyes-purple {
  gap: 32px;
}

.eyes-black {
  gap: 24px;
}

.eyes-orange,
.eyes-yellow {
  gap: 32px;
  transition: all 0.2s ease-out;
}

.eyes-yellow {
  gap: 24px;
}

.mouth {
  position: absolute;
  width: 80px;
  height: 4px;
  background-color: #2d2d2d;
  border-radius: 9999px;
  transition: all 0.2s ease-out;
}

/* 响应式：小屏幕缩小角色 */
@media (max-width: 1024px) {
  .animated-characters {
    transform: scale(0.7);
    transform-origin: center bottom;
  }
}

@media (max-width: 768px) {
  .animated-characters {
    transform: scale(0.5);
  }
}
</style>
