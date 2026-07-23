<template>
  <div class="login-view" :class="{ 'has-alert': serverStatus === 'error' }">
    <!-- 服务状态提示 -->
    <div v-if="serverStatus === 'error'" class="server-status-alert">
      <div class="alert-content">
        <exclamation-circle-icon class="alert-icon" size="20" />
        <div class="alert-text">
          <div class="alert-title">服务端连接失败</div>
          <div class="alert-message">{{ serverError }}</div>
        </div>
        <a-button type="link" size="small" @click="checkServerHealth" :loading="healthChecking">
          重试
        </a-button>
      </div>
    </div>

    <!-- 双栏布局：左动画角色栏 + 右表单栏 -->
    <div class="login-container">
      <!-- 左侧：动画角色栏（lg 以上显示） -->
      <aside class="login-left">
        <!-- 顶部 logo -->
        <div class="left-brand" @click="goHome">
          <img v-if="brandLogo" :src="brandLogo" alt="logo" class="brand-logo" />
          <div v-else class="brand-logo-placeholder">S</div>
          <span class="brand-name">{{ brandOrgName || brandName }}</span>
        </div>

        <!-- 中间：动画角色 -->
        <div class="left-characters">
          <AnimatedCharacters
            :is-typing="isTyping"
            :show-password="showPassword"
            :password-length="passwordLength"
          />
        </div>

        <!-- 底部：协议链接 -->
        <div class="left-footer">
          <a v-if="userAgreementUrl" :href="userAgreementUrl" target="_blank" rel="noopener noreferrer">
            用户协议
          </a>
          <a v-if="privacyPolicyUrl" :href="privacyPolicyUrl" target="_blank" rel="noopener noreferrer">
            隐私协议
          </a>
        </div>

        <!-- 装饰背景 -->
        <div class="left-grid-bg"></div>
        <div class="left-blob left-blob-1"></div>
        <div class="left-blob left-blob-2"></div>
      </aside>

      <!-- 右侧：表单栏 -->
      <main class="login-right">
        <!-- 移动端 logo（lg 以下显示） -->
        <div class="mobile-brand" @click="goHome">
          <img v-if="brandLogo" :src="brandLogo" alt="logo" class="brand-logo" />
          <div v-else class="brand-logo-placeholder">S</div>
          <span class="brand-name">{{ brandOrgName || brandName }}</span>
        </div>

        <!-- 表单卡片 -->
        <div class="form-card">
          <!-- 标题 -->
          <header class="form-header">
            <h1 v-if="isFirstRun" class="form-title">系统初始化</h1>
            <h1 v-else class="form-title">欢迎回来</h1>
            <p class="form-subtitle">
              {{ isFirstRun ? '请创建超级管理员账户' : '请输入您的登录信息' }}
            </p>
          </header>

          <!-- 初始化管理员表单 -->
          <div v-if="isFirstRun" class="form-content">
            <a-form :model="adminForm" @finish="handleInitialize" layout="vertical">
              <a-form-item
                label="UID"
                name="uid"
                :rules="[
                  { required: true, message: '请输入UID' },
                  {
                    pattern: /^[a-zA-Z0-9_]+$/,
                    message: 'UID只能包含字母、数字和下划线'
                  },
                  {
                    min: 3,
                    max: 20,
                    message: 'UID长度必须在3-20个字符之间'
                  }
                ]"
              >
                <a-input
                  v-model:value="adminForm.uid"
                  placeholder="请输入UID（3-20个字符）"
                  :maxlength="20"
                />
              </a-form-item>

              <a-form-item
                label="手机号（可选）"
                name="phone_number"
                :rules="[
                  {
                    validator: async (rule, value) => {
                      if (!value || value.trim() === '') {
                        return // 空值允许
                      }
                      const phoneRegex = /^1[3-9]\d{9}$/
                      if (!phoneRegex.test(value)) {
                        throw new Error('请输入正确的手机号格式')
                      }
                    }
                  }
                ]"
              >
                <a-input
                  v-model:value="adminForm.phone_number"
                  placeholder="可用于登录，可不填写"
                  :max-length="11"
                />
              </a-form-item>

              <a-form-item
                label="密码"
                name="password"
                :rules="[{ required: true, message: '请输入密码' }]"
              >
                <a-input
                  v-model:value="adminForm.password"
                  :type="showPassword ? 'text' : 'password'"
                  placeholder="请输入密码"
                >
                  <template #prefix>
                    <lock-icon size="18" />
                  </template>
                  <template #suffix>
                    <button
                      type="button"
                      class="password-toggle"
                      @click="showPassword = !showPassword"
                      tabindex="-1"
                    >
                      <EyeOff v-if="showPassword" :size="18" />
                      <Eye v-else :size="18" />
                    </button>
                  </template>
                </a-input>
              </a-form-item>

              <a-form-item
                label="确认密码"
                name="confirmPassword"
                :rules="[
                  { required: true, message: '请确认密码' },
                  { validator: validateConfirmPassword }
                ]"
              >
                <a-input
                  v-model:value="adminForm.confirmPassword"
                  :type="showPassword ? 'text' : 'password'"
                  placeholder="请再次输入密码"
                >
                  <template #prefix>
                    <lock-icon size="18" />
                  </template>
                  <template #suffix>
                    <button
                      type="button"
                      class="password-toggle"
                      @click="showPassword = !showPassword"
                      tabindex="-1"
                    >
                      <EyeOff v-if="showPassword" :size="18" />
                      <Eye v-else :size="18" />
                    </button>
                  </template>
                </a-input>
              </a-form-item>

              <a-form-item v-if="showAgreementConsent" class="agreement-form-item">
                <div class="agreement-row">
                  <a-checkbox v-model:checked="agreementAccepted">
                    登录即代表同意
                    <a
                      class="agreement-link"
                      :href="userAgreementUrl"
                      target="_blank"
                      rel="noopener noreferrer"
                      @click.stop
                      >《用户协议》</a
                    >
                    <a
                      class="agreement-link"
                      :href="privacyPolicyUrl"
                      target="_blank"
                      rel="noopener noreferrer"
                      @click.stop
                      >《隐私协议》</a
                    >
                  </a-checkbox>
                </div>
              </a-form-item>

              <a-form-item>
                <a-button type="primary" html-type="submit" :loading="loading" block size="large">
                  创建管理员账户
                </a-button>
              </a-form-item>
            </a-form>
          </div>

          <!-- 登录表单 -->
          <div v-else class="form-content">
            <a-form :model="loginForm" @finish="handleLogin" layout="vertical">
              <a-form-item
                label="登录账号"
                name="loginId"
                :rules="[{ required: true, message: '请输入UID或手机号' }]"
              >
                <a-input
                  v-model:value="loginForm.loginId"
                  placeholder="UID或手机号"
                  @focus="isTyping = true"
                  @blur="isTyping = false"
                >
                  <template #prefix>
                    <user-icon size="18" />
                  </template>
                </a-input>
              </a-form-item>

              <a-form-item
                label="密码"
                name="password"
                :rules="[{ required: true, message: '请输入密码' }]"
              >
                <a-input
                  v-model:value="loginForm.password"
                  :type="showPassword ? 'text' : 'password'"
                  placeholder="请输入密码"
                >
                  <template #prefix>
                    <lock-icon size="18" />
                  </template>
                  <template #suffix>
                    <button
                      type="button"
                      class="password-toggle"
                      @click="showPassword = !showPassword"
                      tabindex="-1"
                    >
                      <EyeOff v-if="showPassword" :size="18" />
                      <Eye v-else :size="18" />
                    </button>
                  </template>
                </a-input>
              </a-form-item>

              <a-form-item v-if="showAgreementConsent" class="agreement-form-item">
                <div class="agreement-row">
                  <a-checkbox v-model:checked="agreementAccepted">
                    登录即代表同意
                    <a
                      class="agreement-link"
                      :href="userAgreementUrl"
                      target="_blank"
                      rel="noopener noreferrer"
                      @click.stop
                      >《用户协议》</a
                    >
                    <a
                      class="agreement-link"
                      :href="privacyPolicyUrl"
                      target="_blank"
                      rel="noopener noreferrer"
                      @click.stop
                      >《隐私协议》</a
                    >
                  </a-checkbox>
                </div>
              </a-form-item>

              <a-form-item>
                <a-button
                  type="primary"
                  html-type="submit"
                  :loading="loading"
                  :disabled="isLocked"
                  block
                  size="large"
                >
                  <span v-if="isLocked">账户已锁定 {{ formatTime(lockRemainingTime) }}</span>
                  <span v-else>登录</span>
                </a-button>
              </a-form-item>
            </a-form>

            <!-- OIDC 登录选项 -->
            <div v-if="oidcChecking || oidcEnabled" class="third-party-login">
              <div class="divider">
                <span>或使用以下方式登录</span>
              </div>
              <div class="login-icons">
                <div v-if="oidcChecking" class="login-skeleton">
                  <a-skeleton-button block size="large" :active="true" />
                </div>
                <a-button
                  v-else
                  type="default"
                  size="large"
                  block
                  :loading="oidcLoading"
                  @click="handleOIDCLogin"
                >
                  <template #icon>
                    <key-icon size="18" />
                  </template>
                  {{ oidcButtonText }}
                </a-button>
              </div>
            </div>
          </div>

          <!-- 错误提示 -->
          <div v-if="errorMessage" class="error-message">
            {{ errorMessage }}
          </div>
        </div>

        <!-- 页脚 -->
        <footer class="right-footer">
          <div class="footer-links">
            <a href="https://github.com/Ember452" target="_blank">联系我们</a>
            <span class="divider">|</span>
            <a href="https://github.com/Ember452/starring" target="_blank">使用帮助</a>
          </div>
          <div class="copyright">
            &copy; {{ new Date().getFullYear() }} {{ brandName }}. All Rights Reserved.
          </div>
        </footer>
      </main>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted, onUnmounted, computed } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useUserStore } from '@/stores/user'
import { useInfoStore } from '@/stores/info'
import { useAgentStore } from '@/stores/agent'
import { message } from 'ant-design-vue'
import { healthApi } from '@/apis/system_api'
import { authApi } from '@/apis/auth_api'
import {
  User as UserIcon,
  Lock as LockIcon,
  Key as KeyIcon,
  AlertCircle as ExclamationCircleIcon,
  Eye,
  EyeOff
} from 'lucide-vue-next'
import { tryAutoStartOIDC, sanitizeRedirect } from '@/utils/oidcAutoStart'
import AnimatedCharacters from '@/components/auth/AnimatedCharacters.vue'

const router = useRouter()
const route = useRoute()
const userStore = useUserStore()
const infoStore = useInfoStore()
const agentStore = useAgentStore()

// 品牌展示数据
const brandLogo = computed(() => {
  return infoStore.organization?.logo || ''
})
const brandOrgName = computed(() => {
  return infoStore.organization?.name?.trim() || ''
})
const brandName = computed(() => {
  const orgName = brandOrgName.value
  const brandNameRaw = infoStore.branding?.name?.trim() || 'StarRing'

  if (orgName && brandNameRaw && orgName !== brandNameRaw) {
    return brandNameRaw
  }

  return orgName || brandNameRaw
})
const userAgreementUrl = computed(() => {
  return infoStore.footer?.user_agreement_url?.trim() || ''
})
const privacyPolicyUrl = computed(() => {
  return infoStore.footer?.privacy_policy_url?.trim() || ''
})
const showAgreementConsent = computed(() => {
  return Boolean(userAgreementUrl.value && privacyPolicyUrl.value)
})

// 状态
const isFirstRun = ref(false)
const loading = ref(false)
const errorMessage = ref('')
const agreementAccepted = ref(false)
const serverStatus = ref('loading')
const serverError = ref('')
const healthChecking = ref(false)

// OIDC 相关状态
const oidcEnabled = ref(false)
const oidcLoading = ref(false)
const oidcChecking = ref(true)
const oidcButtonText = ref('OIDC 登录')

// 登录锁定相关状态
const isLocked = ref(false)
const lockRemainingTime = ref(0)
const lockCountdown = ref(null)

// 动画角色联动状态
const showPassword = ref(false)
const isTyping = ref(false)
const passwordLength = computed(() => loginForm.password.length)

// 登录表单
const loginForm = reactive({
  loginId: '', // 支持uid或phone_number登录
  password: ''
})

// 管理员初始化表单
const adminForm = reactive({
  uid: '', // 改为直接输入uid
  password: '',
  confirmPassword: '',
  phone_number: '' // 手机号字段（可选）
})

const goHome = () => {
  router.push('/')
}

// 清理倒计时器
const clearLockCountdown = () => {
  if (lockCountdown.value) {
    clearInterval(lockCountdown.value)
    lockCountdown.value = null
  }
}

// 启动锁定倒计时
const startLockCountdown = (remainingSeconds) => {
  clearLockCountdown()
  isLocked.value = true
  lockRemainingTime.value = remainingSeconds

  lockCountdown.value = setInterval(() => {
    lockRemainingTime.value--
    if (lockRemainingTime.value <= 0) {
      clearLockCountdown()
      isLocked.value = false
      errorMessage.value = ''
    }
  }, 1000)
}

// 格式化时间显示
const formatTime = (seconds) => {
  if (seconds < 60) {
    return `${seconds}秒`
  } else if (seconds < 3600) {
    const minutes = Math.floor(seconds / 60)
    const remainingSeconds = seconds % 60
    return `${minutes}分${remainingSeconds}秒`
  } else if (seconds < 86400) {
    const hours = Math.floor(seconds / 3600)
    const minutes = Math.floor((seconds % 3600) / 60)
    return `${hours}小时${minutes}分钟`
  } else {
    const days = Math.floor(seconds / 86400)
    const hours = Math.floor((seconds % 86400) / 3600)
    return `${days}天${hours}小时`
  }
}

// 密码确认验证
const validateConfirmPassword = async (rule, value) => {
  if (value === '') {
    throw new Error('请确认密码')
  }
  if (value !== adminForm.password) {
    throw new Error('两次输入的密码不一致')
  }
}

const ensureAgreementAccepted = () => {
  if (!showAgreementConsent.value || agreementAccepted.value) {
    return true
  }

  const warningMessage = '请先阅读并同意《用户协议》《隐私协议》'
  message.warning(warningMessage)
  return false
}

// 处理登录
const handleLogin = async () => {
  // 如果当前被锁定，不允许登录
  if (isLocked.value) {
    message.warning(`账户被锁定，请等待 ${formatTime(lockRemainingTime.value)}`)
    return
  }

  if (!ensureAgreementAccepted()) {
    return
  }

  try {
    loading.value = true
    errorMessage.value = ''
    clearLockCountdown()

    await userStore.login({
      loginId: loginForm.loginId,
      password: loginForm.password
    })

    message.success('登录成功')

    // 获取重定向路径
    const redirectPath = sessionStorage.getItem('redirect') || '/'
    sessionStorage.removeItem('redirect') // 清除重定向信息

    // 根据用户角色决定重定向目标
    if (redirectPath === '/') {
      // 统一跳转到聊天页面（管理员与普通用户共享同一聊天界面）
      try {
        await agentStore.initialize()
        router.push('/agent')
      } catch (error) {
        console.error('获取智能体信息失败:', error)
        router.push('/agent')
      }
    } else {
      // 跳转到其他预设的路径
      router.push(redirectPath)
    }
  } catch (error) {
    console.error('登录失败:', error)

    // 检查是否是锁定错误（HTTP 423）
    if (error.status === 423) {
      // 尝试从响应头中获取剩余时间
      let remainingTime = 0
      if (error.headers && error.headers.get) {
        const lockRemainingHeader = error.headers.get('X-Lock-Remaining')
        if (lockRemainingHeader) {
          remainingTime = parseInt(lockRemainingHeader)
        }
      }

      // 如果没有从头中获取到，尝试从错误消息中解析
      if (remainingTime === 0) {
        const lockTimeMatch = error.message.match(/(\d+)\s*秒/)
        if (lockTimeMatch) {
          remainingTime = parseInt(lockTimeMatch[1])
        }
      }

      if (remainingTime > 0) {
        startLockCountdown(remainingTime)
        errorMessage.value = `由于多次登录失败，账户已被锁定 ${formatTime(remainingTime)}`
      } else {
        errorMessage.value = error.message || '账户被锁定，请稍后再试'
      }
    } else {
      errorMessage.value = error.message || '登录失败，请检查用户名和密码'
    }
  } finally {
    loading.value = false
  }
}

// 处理 OIDC 登录
const handleOIDCLogin = async () => {
  if (!ensureAgreementAccepted()) {
    return
  }

  try {
    oidcLoading.value = true
    errorMessage.value = ''

    // 获取 OIDC 登录 URL
    const response = await authApi.getOIDCLoginUrl()
    if (response.login_url) {
      // 保存当前路径，以便登录后返回
      const redirectPath =
        sessionStorage.getItem('redirect') || router.currentRoute.value.query.redirect || '/'
      sessionStorage.setItem('oidc_redirect', redirectPath)

      // 跳转到 OIDC Provider
      window.location.href = response.login_url
    } else {
      errorMessage.value = '获取 OIDC 登录地址失败'
    }
  } catch (error) {
    console.error('OIDC 登录失败:', error)
    errorMessage.value = error.message || 'OIDC 登录失败，请重试'
  } finally {
    oidcLoading.value = false
  }
}

// 检查 OIDC 配置
const checkOIDCConfig = async () => {
  oidcChecking.value = true
  try {
    const config = await authApi.getOIDCConfig()
    oidcEnabled.value = config.enabled
    if (config.provider_name) {
      oidcButtonText.value = config.provider_name
    }
    return config
  } catch (error) {
    console.error('检查 OIDC 配置失败:', error)
    oidcEnabled.value = false
    return null
  } finally {
    oidcChecking.value = false
  }
}

// 处理初始化管理员
const handleInitialize = async () => {
  if (!ensureAgreementAccepted()) {
    return
  }

  try {
    loading.value = true
    errorMessage.value = ''

    if (adminForm.password !== adminForm.confirmPassword) {
      errorMessage.value = '两次输入的密码不一致'
      return
    }

    await userStore.initialize({
      uid: adminForm.uid,
      password: adminForm.password,
      phone_number: adminForm.phone_number || null // 空字符串转为null
    })

    message.success('管理员账户创建成功')
    router.push('/')
  } catch (error) {
    console.error('初始化失败:', error)
    errorMessage.value = error.message || '初始化失败，请重试'
  } finally {
    loading.value = false
  }
}

// 检查是否是首次运行
const checkFirstRunStatus = async () => {
  try {
    loading.value = true
    const isFirst = await userStore.checkFirstRun()
    isFirstRun.value = isFirst
  } catch (error) {
    console.error('检查首次运行状态失败:', error)
    errorMessage.value = '系统出错，请稍后重试'
  } finally {
    loading.value = false
  }
}

// 检查服务器健康状态
const checkServerHealth = async () => {
  try {
    healthChecking.value = true
    const response = await healthApi.checkHealth()
    if (response.status === 'ok') {
      serverStatus.value = 'ok'
    } else {
      serverStatus.value = 'error'
      serverError.value = response.message || '服务端状态异常'
    }
  } catch (error) {
    console.error('检查服务器健康状态失败:', error)
    serverStatus.value = 'error'
    serverError.value = error.message || '无法连接到服务端，请检查网络连接'
  } finally {
    healthChecking.value = false
  }
}

// 组件挂载时
onMounted(async () => {
  // 如果已登录，按 redirect 参数跳转（不固定跳首页）
  if (userStore.isLoggedIn) {
    router.push(sanitizeRedirect(route.query.redirect))
    return
  }

  // 显示 OIDC 认证失败的错误信息（由后端重定向携带）
  if (route.query.oidc_error) {
    errorMessage.value = String(route.query.oidc_error)
  }

  // 首先检查服务器健康状态
  await checkServerHealth()

  // 检查是否是首次运行
  await checkFirstRunStatus()

  // 如果处于首次运行状态，不需要 OIDC 自动登录
  if (isFirstRun.value) {
    return
  }

  // 检查 OIDC 配置完成后，尝试自动触发 OIDC 登录（跨系统跳转场景）
  const config = await checkOIDCConfig()
  if (config && config.enabled) {
    const autoStarted = await tryAutoStartOIDC(async () => await authApi.getOIDCLoginUrl(), config)
    // 如果已发起 OIDC 跳转，页面会被重定向，不需要继续
    if (autoStarted) return
  }
})

// 组件卸载时清理定时器
onUnmounted(() => {
  clearLockCountdown()
})
</script>

<style lang="less" scoped>
.login-view {
  min-height: 100vh;
  width: 100%;
  position: relative;
  background-color: var(--color-bg-container);

  &.has-alert {
    padding-top: 60px;
  }
}

/* 双栏布局 */
.login-container {
  display: grid;
  grid-template-columns: 1fr 1fr;
  min-height: 100vh;

  @media (max-width: 1024px) {
    grid-template-columns: 1fr;
  }
}

/* 左侧动画角色栏 */
.login-left {
  position: relative;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  padding: 48px;
  background: linear-gradient(135deg, #9ca3af 0%, #6b7280 50%, #4b5563 100%);
  color: #fff;
  overflow: hidden;

  @media (max-width: 1024px) {
    display: none;
  }

  :root.dark & {
    background: linear-gradient(135deg, #1f2937 0%, #111827 50%, #030712 100%);
    color: #f3f4f6;
  }
}

.left-brand {
  position: relative;
  z-index: 2;
  display: flex;
  align-items: center;
  gap: 12px;
  cursor: pointer;
  font-size: 18px;
  font-weight: 600;

  .brand-logo {
    width: 32px;
    height: 32px;
    object-fit: contain;
    background: rgba(255, 255, 255, 0.1);
    backdrop-filter: blur(4px);
    padding: 4px;
    border-radius: 8px;
  }

  .brand-logo-placeholder {
    width: 32px;
    height: 32px;
    background: rgba(255, 255, 255, 0.15);
    backdrop-filter: blur(4px);
    border-radius: 8px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-weight: 700;
  }
}

.left-characters {
  position: relative;
  z-index: 2;
  display: flex;
  align-items: flex-end;
  justify-content: center;
  flex: 1;
  min-height: 400px;
}

.left-footer {
  position: relative;
  z-index: 2;
  display: flex;
  gap: 32px;
  font-size: 13px;
  opacity: 0.8;

  a {
    color: inherit;
    text-decoration: none;
    transition: opacity 0.2s;

    &:hover {
      opacity: 1;
    }
  }
}

/* 装饰背景 */
.left-grid-bg {
  position: absolute;
  inset: 0;
  background-image:
    linear-gradient(rgba(255, 255, 255, 0.05) 1px, transparent 1px),
    linear-gradient(90deg, rgba(255, 255, 255, 0.05) 1px, transparent 1px);
  background-size: 20px 20px;
  pointer-events: none;
}

.left-blob {
  position: absolute;
  border-radius: 50%;
  filter: blur(64px);
  pointer-events: none;
}

.left-blob-1 {
  top: 25%;
  right: 25%;
  width: 256px;
  height: 256px;
  background: rgba(156, 163, 175, 0.2);
}

.left-blob-2 {
  bottom: 25%;
  left: 25%;
  width: 384px;
  height: 384px;
  background: rgba(209, 213, 219, 0.2);
}

/* 右侧表单栏 */
.login-right {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 48px 24px;
  background: var(--color-bg-container);

  @media (max-width: 1024px) {
    min-height: 100vh;
  }
}

.mobile-brand {
  display: none;

  @media (max-width: 1024px) {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 48px;
    font-size: 18px;
    font-weight: 600;
    color: var(--color-text);
    cursor: pointer;
  }

  .brand-logo {
    width: 32px;
    height: 32px;
    object-fit: contain;
  }

  .brand-logo-placeholder {
    width: 32px;
    height: 32px;
    background: var(--main-color);
    color: #fff;
    border-radius: 8px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-weight: 700;
  }
}

/* 表单卡片 */
.form-card {
  width: 100%;
  max-width: 420px;
}

.form-header {
  text-align: center;
  margin-bottom: 40px;

  .form-title {
    font-size: 30px;
    font-weight: 700;
    color: var(--color-text);
    margin: 0 0 8px;
    letter-spacing: -0.02em;
  }

  .form-subtitle {
    font-size: 14px;
    color: var(--gray-600);
    margin: 0;
  }
}

.form-content {
  :deep(.ant-form-item-label > label) {
    color: var(--color-text);
    font-size: 14px;
  }

  /* affix-wrapper（prefix/suffix 场景）外层样式 */
  :deep(.ant-input-affix-wrapper) {
    padding: 0 14px;
    border-radius: 10px;
    height: 48px;
    font-size: 15px;
    color: var(--color-text);
    background-color: var(--color-bg-container);
    border-color: var(--gray-200);
    display: flex;
    align-items: center;

    &:hover,
    &-focused,
    &:focus-within {
      border-color: var(--main-color);
    }
  }

  /* affix-wrapper 内部 input：必须放在 :deep() 参数内才会生效 */
  :deep(.ant-input-affix-wrapper .ant-input) {
    height: auto;
    color: var(--color-text);
    background: transparent;
    box-shadow: none;
  }

  /* 无 prefix/suffix 的纯 input */
  :deep(.ant-input:not(.ant-input-affix-wrapper input)) {
    height: 48px;
    border-radius: 10px;
    font-size: 15px;
    color: var(--color-text);
    background-color: var(--color-bg-container);
    border-color: var(--gray-200);

    &:hover,
    &:focus {
      border-color: var(--main-color);
    }
  }

  :deep(.ant-input::placeholder) {
    color: var(--gray-500);
  }

  :deep(.ant-btn-primary) {
    height: 48px;
    font-size: 15px;
    font-weight: 600;
    border-radius: 10px;
  }

  :deep(.ant-input-prefix) {
    margin-right: 10px;
    color: var(--gray-400);
  }
}

/* 密码显示切换按钮 */
.password-toggle {
  background: transparent;
  border: none;
  padding: 4px;
  cursor: pointer;
  color: var(--gray-400);
  display: flex;
  align-items: center;
  justify-content: center;
  transition: color 0.2s;

  &:hover {
    color: var(--main-color);
  }
}

/* 协议同意 */
.agreement-form-item {
  margin-bottom: 12px;
}

.agreement-row {
  font-size: 13px;
  color: var(--gray-600);
  line-height: 1.6;

  :deep(.ant-checkbox-wrapper) {
    display: inline-flex;
    align-items: flex-start;
    color: var(--gray-600);
  }

  :deep(.ant-checkbox + span) {
    padding-inline-start: 8px;
  }
}

.agreement-link {
  color: var(--main-color);

  &:hover {
    text-decoration: underline;
  }
}

/* 第三方登录 */
.third-party-login {
  margin-top: 20px;

  .divider {
    position: relative;
    text-align: center;
    margin: 20px 0 16px;

    &::before,
    &::after {
      content: '';
      position: absolute;
      top: 50%;
      width: 35%;
      height: 1px;
      background-color: var(--gray-200);
    }

    &::before {
      left: 0;
    }

    &::after {
      right: 0;
    }

    span {
      display: inline-block;
      padding: 0 12px;
      background: var(--color-bg-container);
      color: var(--gray-500);
      font-size: 12px;
    }
  }

  .login-icons {
    :deep(.ant-btn) {
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      border-color: var(--gray-200);
      color: var(--color-text);
      background-color: var(--color-bg-container);

      &:hover {
        border-color: var(--main-color);
        color: var(--main-color);
        background-color: var(--main-50);
      }
    }
  }

  .login-skeleton {
    :deep(.ant-skeleton-button) {
      width: 100% !important;
      height: 48px;
      border-radius: 10px;
    }
  }
}

/* 错误提示 */
.error-message {
  margin-top: 16px;
  padding: 12px 14px;
  background-color: var(--color-error-50);
  border: 1px solid color-mix(in srgb, var(--color-error-500) 25%, transparent);
  border-radius: 8px;
  color: var(--color-error-700);
  font-size: 13px;
  text-align: center;
}

/* 页脚 */
.right-footer {
  margin-top: 48px;
  text-align: center;
}

.footer-links {
  display: flex;
  justify-content: center;
  align-items: center;
  gap: 16px;
  margin-bottom: 8px;

  a {
    color: var(--gray-600);
    font-size: 13px;
    text-decoration: none;
    transition: color 0.2s;

    &:hover {
      color: var(--main-color);
    }
  }

  .divider {
    color: var(--gray-300);
    font-size: 12px;
  }
}

.copyright {
  font-size: 12px;
  color: var(--gray-500);
}

/* 服务状态警告条 */
.server-status-alert {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  padding: 12px 20px;
  background: var(--color-error-500);
  color: #fff;
  z-index: 1000;

  .alert-content {
    display: flex;
    align-items: center;
    max-width: 1500px;
    margin: 0 auto;

    .alert-icon {
      font-size: 20px;
      margin-right: 12px;
      color: #fff;
    }

    .alert-text {
      flex: 1;

      .alert-title {
        font-weight: 600;
        font-size: 16px;
        margin-bottom: 2px;
      }

      .alert-message {
        font-size: 14px;
        opacity: 0.9;
      }
    }

    :deep(.ant-btn-link) {
      color: #fff;
      border-color: #fff;

      &:hover {
        color: #fff;
        background-color: rgba(255, 255, 255, 0.1);
      }
    }
  }
}

/* 响应式 */
@media (max-width: 768px) {
  .login-right {
    padding: 32px 20px;
  }

  .form-header .form-title {
    font-size: 24px;
  }
}
</style>
