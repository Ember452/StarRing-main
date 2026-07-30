<template>
  <div class="home-container">
    <!-- 加载中状态 -->
    <div v-if="isLoading" class="loading-container">
      <a-spin size="large" />
      <p class="loading-text">正在连接服务...</p>
    </div>

    <!-- 错误状态 -->
    <div v-else-if="error" class="error-container">
      <a-result status="error" :title="error.title" :sub-title="error.message">
        <template #extra>
          <a-button type="primary" @click="retryLoad">重试</a-button>
          <a-button :href="faqUrl" target="_blank" rel="noopener noreferrer">常见问题</a-button>
        </template>
      </a-result>
    </div>

    <!-- 正常内容 -->
    <template v-else>
      <!-- 氛围装饰背景 -->
      <div class="ambient" aria-hidden="true" ref="ambientRef">
        <span class="orb orb-1" ref="orb1Ref"></span>
        <span class="orb orb-2" ref="orb2Ref"></span>
        <span class="orb orb-3" ref="orb3Ref"></span>
        <div class="grid-mesh"></div>
      </div>

      <header class="glass-header">
        <div class="logo">
          <img
            :src="infoStore.organization.logo"
            :alt="infoStore.organization.name"
            class="logo-img"
          />
          <span class="logo-text">{{ infoStore.organization.name }}</span>
        </div>
        <div class="header-actions">
          <a
            class="github-link"
            href="https://github.com/Ember452/starring"
            target="_blank"
            rel="noopener noreferrer"
            aria-label="GitHub"
          >
            <svg height="20" width="20" viewBox="0 0 16 16" version="1.1">
              <path
                fill-rule="evenodd"
                d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"
              ></path>
            </svg>
          </a>
          <UserInfoComponent :show-button="true" />
        </div>
      </header>

      <main class="hero-section">
        <div class="hero-layout">
          <div class="hero-content" ref="heroContentRef">
            <p v-if="typedBadge" class="hero-badge anime-fade-up" :class="{ typing: isBadgeTyping }">
              <span class="badge-dot"></span>
              <template v-if="badgeParts.number">
                <span>{{ badgeParts.prefix }}</span>
                <a
                  class="hero-badge-link"
                  :href="repoUrl"
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  <span class="hero-badge-number">{{ badgeParts.number }}</span>
                </a>
                <span>{{ badgeParts.suffix }}</span>
              </template>
              <template v-else>{{ typedBadge }}</template>
            </p>
            <h1 class="title anime-fade-up">{{ infoStore.branding.title }}</h1>
            <Transition name="subtitle-switch" mode="out-in">
              <p v-if="currentSubtitle" class="subtitle" :key="currentSubtitle">
                {{ currentSubtitle }}
              </p>
            </Transition>
            <p class="pain-point anime-fade-up">
              解决大模型幻觉、资料碎片化、关联信息挖掘难题
            </p>
            <div class="hero-actions anime-fade-up">
              <button class="button-base primary" @click="goToChat">
                <span>开始体验</span>
                <ArrowRight :size="18" />
              </button>
              <a
                class="button-base secondary"
                href="https://ember452.github.io/starring/"
                target="_blank"
                rel="noopener noreferrer"
              >
                <BookText :size="18" />
                <span>查看文档</span>
              </a>
            </div>
          </div>

          <aside class="hero-visual" ref="heroVisualRef">
            <div class="visual-card">
              <div class="visual-glow" aria-hidden="true"></div>
              <svg
                class="graph-watermark"
                viewBox="0 0 240 200"
                fill="none"
                aria-hidden="true"
                xmlns="http://www.w3.org/2000/svg"
              >
                <g stroke="currentColor" stroke-width="2">
                  <line x1="120" y1="100" x2="48" y2="44" />
                  <line x1="120" y1="100" x2="200" y2="56" />
                  <line x1="120" y1="100" x2="56" y2="156" />
                  <line x1="120" y1="100" x2="180" y2="150" />
                  <line x1="48" y1="44" x2="200" y2="56" />
                </g>
                <g fill="currentColor">
                  <circle cx="120" cy="100" r="11" />
                  <circle cx="48" cy="44" r="7" />
                  <circle cx="200" cy="56" r="8" />
                  <circle cx="56" cy="156" r="6" />
                  <circle cx="180" cy="150" r="9" />
                </g>
              </svg>

              <div class="flow-diagram" ref="flowDiagramRef">
                <div class="flow-row">
                  <div class="flow-node">
                    <span class="flow-icon"><Workflow :size="22" /></span>
                    <span class="flow-name">智能体 Harness</span>
                    <span class="flow-desc">自定义 Prompt 调度</span>
                  </div>

                  <div class="flow-link" aria-hidden="true">
                    <span class="flow-rail"></span>
                    <span
                      class="flow-dot flow-dot--fwd"
                      v-for="n in 2"
                      :key="`f1${n}`"
                      :style="{ '--i': n - 1 }"
                    ></span>
                    <span
                      class="flow-dot flow-dot--back"
                      v-for="n in 2"
                      :key="`b1${n}`"
                      :style="{ '--i': n - 1 }"
                    ></span>
                  </div>

                  <div class="flow-node flow-node--hub">
                    <span class="flow-icon flow-icon--hub">
                      <span class="hub-ring"></span>
                      <Sparkles :size="24" />
                    </span>
                    <span class="flow-name">RAG 引擎</span>
                    <span class="flow-desc">向量 + 图谱混合检索</span>
                  </div>

                  <div class="flow-link" aria-hidden="true">
                    <span class="flow-rail"></span>
                    <span
                      class="flow-dot flow-dot--fwd"
                      v-for="n in 2"
                      :key="`f2${n}`"
                      :style="{ '--i': n - 1 }"
                    ></span>
                    <span
                      class="flow-dot flow-dot--back"
                      v-for="n in 2"
                      :key="`b2${n}`"
                      :style="{ '--i': n - 1 }"
                    ></span>
                  </div>

                  <div class="flow-node">
                    <span class="flow-icon"><Library :size="22" /></span>
                    <span class="flow-name">知识库</span>
                    <span class="flow-desc">多格式文档解析</span>
                  </div>
                </div>

                <p class="flow-caption">智能体发起检索 · 引擎融合向量与图谱 · 召回知识增强生成</p>
              </div>

              <div class="stat-row" v-if="realtimeStats.length">
                <div class="stat-item" v-for="stat in realtimeStats" :key="stat.key">
                  <span class="stat-item-value">
                    <component :is="stat.icon" :size="15" />
                    {{ stat.value }}
                  </span>
                  <span class="stat-item-label">{{ stat.label }}</span>
                </div>
              </div>
            </div>
          </aside>
        </div>
      </main>

      <!-- 功能亮点卡片区域 -->
      <section class="features-section">
        <div class="features-container">
          <h2 class="features-title">核心能力</h2>
          <p class="features-subtitle">从文档解析到知识检索，一站式智能知识库解决方案</p>

          <div class="features-grid" ref="featuresGridRef">
            <div class="feature-card">
              <div class="feature-icon">
                <FileText :size="24" />
              </div>
              <h3 class="feature-title">多格式文档解析</h3>
              <p class="feature-desc">
                支持 PDF、Word、Markdown、Excel 等多种格式，智能提取文本与结构化信息
              </p>
            </div>

            <div class="feature-card">
              <div class="feature-icon">
                <Database :size="24" />
              </div>
              <h3 class="feature-title">向量 + 图谱双引擎</h3>
              <p class="feature-desc">
                融合向量检索与知识图谱关联，召回更精准、上下文更完整
              </p>
            </div>

            <div class="feature-card">
              <div class="feature-icon">
                <Bot :size="24" />
              </div>
              <h3 class="feature-title">智能体调度</h3>
              <p class="feature-desc">
                自定义 Prompt 编排，多模型兼容，灵活适配不同业务场景
              </p>
            </div>

            <div class="feature-card">
              <div class="feature-icon">
                <Shield :size="24" />
              </div>
              <h3 class="feature-title">本地私有化部署</h3>
              <p class="feature-desc">
                Docker 一键启动，数据不出本地，满足企业数据安全合规需求
              </p>
            </div>
          </div>
        </div>
      </section>

      <!-- 快速部署流程 + 技术栈 -->
      <section class="deploy-section">
        <div class="deploy-container">
          <h2 class="deploy-title">5 分钟快速上手</h2>
          <p class="deploy-subtitle">从拉取镜像到构建知识库，只需三步</p>

          <div class="deploy-steps" ref="deployStepsRef">
            <div class="deploy-step">
              <div class="step-number">1</div>
              <div class="step-content">
                <h3 class="step-title">拉取 Docker 镜像</h3>
                <code class="step-code">docker pull starring/starring:latest</code>
              </div>
            </div>

            <div class="step-arrow">
              <ArrowRight :size="20" />
            </div>

            <div class="deploy-step">
              <div class="step-number">2</div>
              <div class="step-content">
                <h3 class="step-title">启动容器</h3>
                <code class="step-code">docker compose up -d</code>
              </div>
            </div>

            <div class="step-arrow">
              <ArrowRight :size="20" />
            </div>

            <div class="deploy-step">
              <div class="step-number">3</div>
              <div class="step-content">
                <h3 class="step-title">上传文档构建知识库</h3>
                <code class="step-code">访问 localhost:5173</code>
              </div>
            </div>
          </div>

          <div class="tech-stack" ref="techStackRef">
            <span class="tech-label">技术栈：</span>
            <div class="tech-tags">
              <span class="tech-tag">FastAPI</span>
              <span class="tech-tag">PostgreSQL</span>
              <span class="tech-tag">Milvus</span>
              <span class="tech-tag">Neo4j</span>
              <span class="tech-tag">Vue3</span>
              <span class="tech-tag">LangGraph</span>
            </div>
          </div>
        </div>
      </section>

      <footer class="footer">
        <div class="footer-container">
          <div class="footer-top">
            <div class="footer-brand">
              <div class="footer-logo">
                <img
                  :src="infoStore.organization.logo"
                  :alt="infoStore.organization.name"
                  class="footer-logo-img"
                />
                <span class="footer-logo-text">{{ infoStore.organization.name }}</span>
              </div>
              <p class="footer-desc">融合 RAG 与知识图谱的智能知识库平台</p>
            </div>

            <div class="footer-links">
              <div class="footer-column">
                <h4 class="footer-column-title">开发资源</h4>
                <ul class="footer-link-list">
                  <li>
                    <a href="https://ember452.github.io/starring/" target="_blank" rel="noopener noreferrer">
                      快速上手文档
                    </a>
                  </li>
                  <li>
                    <a href="https://ember452.github.io/starring/api" target="_blank" rel="noopener noreferrer">
                      API 接口文档
                    </a>
                  </li>
                  <li>
                    <a href="https://ember452.github.io/starring/deploy" target="_blank" rel="noopener noreferrer">
                      部署教程
                    </a>
                  </li>
                </ul>
              </div>

              <div class="footer-column">
                <h4 class="footer-column-title">社区渠道</h4>
                <ul class="footer-link-list">
                  <li>
                    <a href="https://github.com/Ember452/starring" target="_blank" rel="noopener noreferrer">
                      GitHub 仓库
                    </a>
                  </li>
                  <li>
                    <a href="https://github.com/Ember452/starring/issues" target="_blank" rel="noopener noreferrer">
                      Issues 反馈
                    </a>
                  </li>
                  <li>
                    <a href="https://github.com/Ember452/starring/discussions" target="_blank" rel="noopener noreferrer">
                      开发交流
                    </a>
                  </li>
                </ul>
              </div>
            </div>
          </div>

          <div class="footer-bottom">
            <p class="copyright">
              {{ infoStore.footer?.copyright || '© 2025 StarRing. All rights reserved.' }}
            </p>
            <div class="footer-meta">
              <span class="footer-badge">MIT License</span>
              <span class="footer-badge">v0.1.0</span>
            </div>
          </div>
        </div>
      </footer>
    </template>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted, watch, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import { useUserStore } from '@/stores/user'
import { useInfoStore } from '@/stores/info'
import { healthApi } from '@/apis/system_api'
import UserInfoComponent from '@/components/UserInfoComponent.vue'
import {
  BookText,
  Star,
  GitFork,
  CircleDot,
  ArrowRight,
  Workflow,
  Library,
  Sparkles,
  FileText,
  Database,
  Bot,
  Shield
} from 'lucide-vue-next'
import { animate, stagger, createTimeline } from 'animejs'

const router = useRouter()
const userStore = useUserStore()
const infoStore = useInfoStore()
const repoUrl = 'https://github.com/Ember452/starring'
const faqUrl = 'https://ember452.github.io/starring/'

// 加载状态
const isLoading = ref(true)
const error = ref(null)
const typedBadge = ref('')
const isBadgeTyping = ref(false)
const githubStats = ref(null)
let badgeTimer = null
let subtitleTimer = null
let starsFetchController = null

// Anime.js 动画 refs
const ambientRef = ref(null)
const orb1Ref = ref(null)
const orb2Ref = ref(null)
const orb3Ref = ref(null)
const heroContentRef = ref(null)
const heroVisualRef = ref(null)
const featuresGridRef = ref(null)
const deployStepsRef = ref(null)
const techStackRef = ref(null)
const flowDiagramRef = ref(null)

// 动画实例存储
let heroAnimation = null
let featuresAnimation = null
let deployAnimation = null
let techAnimation = null
let orbAnimations = []
let flowAnimation = null

const GITHUB_REPO_API = 'https://api.github.com/repos/Ember452/starring'
const GITHUB_STARS_TIMEOUT = 3000

const formatStars = (count) => {
  if (!Number.isFinite(count) || count <= 0) {
    return ''
  }
  return `${count}`
}

const subtitleIndex = ref(0)

const subtitleOptions = computed(() => {
  const subtitles = infoStore.branding?.subtitles
  if (Array.isArray(subtitles)) {
    const list = subtitles
      .map((item) => (typeof item === 'string' ? item.trim() : ''))
      .filter(Boolean)
    if (list.length) {
      return list
    }
  }

  const fallback = (infoStore.branding?.subtitle || '').trim()
  return fallback ? [fallback] : []
})

const currentSubtitle = computed(() => subtitleOptions.value[subtitleIndex.value] || '')
const badgeParts = computed(() => {
  const text = typedBadge.value || ''
  const match = text.match(/^(.*?)(\d[\d,]*\+?)(\s+GitHub Stars.*)?$/)
  if (!match) {
    return {
      prefix: text,
      number: '',
      suffix: ''
    }
  }

  return {
    prefix: match[1] || '',
    number: match[2] || '',
    suffix: match[3] || ''
  }
})

const stopSubtitleCarousel = () => {
  if (subtitleTimer) {
    clearInterval(subtitleTimer)
    subtitleTimer = null
  }
}

const startSubtitleCarousel = () => {
  stopSubtitleCarousel()
  subtitleIndex.value = 0

  if (subtitleOptions.value.length <= 1) {
    return
  }

  subtitleTimer = setInterval(() => {
    subtitleIndex.value = (subtitleIndex.value + 1) % subtitleOptions.value.length
  }, 2800)
}

const stopStarsFetch = () => {
  if (starsFetchController) {
    starsFetchController.abort()
    starsFetchController = null
  }
}

const fetchGithubRepo = async () => {
  stopStarsFetch()
  const controller = new AbortController()
  starsFetchController = controller
  const timer = setTimeout(() => {
    controller.abort()
  }, GITHUB_STARS_TIMEOUT)

  try {
    const response = await fetch(GITHUB_REPO_API, { signal: controller.signal })
    if (!response.ok) {
      return null
    }

    const data = await response.json()
    return {
      stars: Number(data?.stargazers_count) || 0,
      forks: Number(data?.forks_count) || 0,
      issues: Number(data?.open_issues_count) || 0
    }
  } catch {
    return null
  } finally {
    clearTimeout(timer)
    if (starsFetchController === controller) {
      starsFetchController = null
    }
  }
}

const getHeroBadgeText = (starsCount = null) => {
  const realtimeStars = formatStars(starsCount)
  return realtimeStars ? `已获得 ${realtimeStars} GitHub Stars` : ''
}

const stopBadgeTyping = () => {
  if (badgeTimer) {
    clearInterval(badgeTimer)
    badgeTimer = null
  }
  isBadgeTyping.value = false
}

const startBadgeTyping = (starsCount = null) => {
  stopBadgeTyping()
  const text = getHeroBadgeText(starsCount)
  typedBadge.value = ''

  if (!text) {
    return
  }

  let index = 0
  isBadgeTyping.value = true
  badgeTimer = setInterval(() => {
    index += 1
    typedBadge.value = text.slice(0, index)
    if (index >= text.length) {
      stopBadgeTyping()
    }
  }, 45)
}

const checkHealth = async () => {
  try {
    const response = await healthApi.checkHealth()
    if (response.status !== 'ok') {
      throw new Error('服务不可用')
    }
  } catch (e) {
    error.value = {
      title: '服务连接失败',
      message: '后端服务无法响应，请检查服务是否正常运行'
    }
    throw e
  }
}

const loadData = async () => {
  isLoading.value = true
  error.value = null

  try {
    // 先检查健康状态
    await checkHealth()
    // 健康检查通过后加载配置
    await infoStore.loadInfoConfig()
    startSubtitleCarousel()
    const repo = await fetchGithubRepo()
    githubStats.value = repo
    startBadgeTyping(repo?.stars ?? null)
  } catch (e) {
    console.error('加载失败:', e)
    stopBadgeTyping()
    stopSubtitleCarousel()
    stopStarsFetch()
    typedBadge.value = ''
  } finally {
    isLoading.value = false
    // 等待 DOM 更新后初始化动画
    await nextTick()
    initAnimations()
  }
}

const retryLoad = () => {
  loadData()
}

const goToChat = async () => {
  if (!userStore.isLoggedIn) {
    sessionStorage.setItem('redirect', '/')
    router.push('/login')
    return
  }

  router.push('/agent')
}

onMounted(() => {
  // 加载数据
  loadData()
})

onUnmounted(() => {
  stopBadgeTyping()
  stopSubtitleCarousel()
  stopStarsFetch()

  // 清理动画实例
  if (heroAnimation) heroAnimation.pause()
  if (featuresAnimation) featuresAnimation.pause()
  if (deployAnimation) deployAnimation.pause()
  if (techAnimation) techAnimation.pause()
  if (flowAnimation) flowAnimation.pause()
  orbAnimations.forEach(anim => anim.pause())
})

// 统一初始化所有动画（在数据加载完成后调用）
const initAnimations = () => {
  console.log('[HomeView] 开始初始化动画...')
  console.log('[HomeView] heroContentRef:', heroContentRef.value)
  console.log('[HomeView] heroVisualRef:', heroVisualRef.value)
  
  initHeroAnimation()
  initOrbAnimations()
  initFeaturesAnimation()
  initDeployAnimation()
  initTechAnimation()
  initFlowAnimation()
}

// 初始化 Hero 区域入场动画
const initHeroAnimation = () => {
  if (!heroContentRef.value || !heroVisualRef.value) return

  // 先设置初始状态（隐藏元素）
  const fadeUpElements = heroContentRef.value.querySelectorAll('.anime-fade-up')
  fadeUpElements.forEach(el => {
    el.style.opacity = '0'
    el.style.transform = 'translateY(30px)'
  })
  heroVisualRef.value.style.opacity = '0'
  heroVisualRef.value.style.transform = 'scale(0.9)'

  // Hero 内容交错入场 - 使用正确的 anime.js v4 API
  heroAnimation = animate(fadeUpElements, {
    opacity: { from: 0, to: 1 },
    translateY: { from: 30, to: 0 },
    delay: stagger(100),
    duration: 800,
    ease: 'outCubic'
  })

  // 右侧可视化卡片入场
  animate(heroVisualRef.value, {
    opacity: { from: 0, to: 1 },
    scale: { from: 0.9, to: 1 },
    duration: 1000,
    delay: 200,
    ease: 'outCubic'
  })
}

// 初始化光斑动画（鼠标视差 + 呼吸效果）
const initOrbAnimations = () => {
  const orbs = [orb1Ref.value, orb2Ref.value, orb3Ref.value].filter(Boolean)
  if (orbs.length === 0) return

  // 呼吸动画
  orbs.forEach((orb, index) => {
    const anim = animate(orb, {
      scale: { from: 1, to: 1.1 },
      opacity: { from: 0.3, to: 0.5 },
      duration: 4000 + index * 1000,
      ease: 'inOutSine',
      loop: true,
      alternate: true
    })
    orbAnimations.push(anim)
  })

  // 鼠标视差跟随
  const handleMouseMove = (e) => {
    const centerX = window.innerWidth / 2
    const centerY = window.innerHeight / 2
    const deltaX = (e.clientX - centerX) / centerX
    const deltaY = (e.clientY - centerY) / centerY

    orbs.forEach((orb, index) => {
      const factor = (index + 1) * 15
      animate(orb, {
        translateX: deltaX * factor,
        translateY: deltaY * factor,
        duration: 800,
        ease: 'outCubic'
      })
    })
  }

  window.addEventListener('mousemove', handleMouseMove)
}

// 初始化功能卡片滚动渐入动画
const initFeaturesAnimation = () => {
  if (!featuresGridRef.value) return

  const observer = new IntersectionObserver(
    (entries) => {
      entries.forEach(entry => {
        if (entry.isIntersecting) {
          const cards = featuresGridRef.value.querySelectorAll('.feature-card')
          // 先设置初始状态
          cards.forEach(card => {
            card.style.opacity = '0'
            card.style.transform = 'translateY(40px)'
          })
          featuresAnimation = animate(cards, {
            opacity: { from: 0, to: 1 },
            translateY: { from: 40, to: 0 },
            delay: stagger(120),
            duration: 700,
            ease: 'outCubic'
          })
          observer.unobserve(entry.target)
        }
      })
    },
    { threshold: 0.2 }
  )

  observer.observe(featuresGridRef.value)
}

// 初始化部署步骤动画
const initDeployAnimation = () => {
  if (!deployStepsRef.value) return

  const observer = new IntersectionObserver(
    (entries) => {
      entries.forEach(entry => {
        if (entry.isIntersecting) {
          const steps = deployStepsRef.value.querySelectorAll('.deploy-step')
          const arrows = deployStepsRef.value.querySelectorAll('.step-arrow')

          // 先设置初始状态
          steps.forEach(step => {
            step.style.opacity = '0'
            step.style.transform = 'translateX(-30px)'
          })
          arrows.forEach(arrow => {
            arrow.style.opacity = '0'
            arrow.style.transform = 'scale(0)'
          })

          deployAnimation = createTimeline()
            .add(steps, {
              opacity: { from: 0, to: 1 },
              translateX: { from: -30, to: 0 },
              delay: stagger(150),
              duration: 600,
              ease: 'outCubic'
            })
            .add(arrows, {
              opacity: { from: 0, to: 1 },
              scale: { from: 0, to: 1 },
              delay: stagger(100, { start: 200 }),
              duration: 400,
              ease: 'outBack'
            }, '-=400')

          observer.unobserve(entry.target)
        }
      })
    },
    { threshold: 0.3 }
  )

  observer.observe(deployStepsRef.value)
}

// 初始化技术栈标签动画
const initTechAnimation = () => {
  if (!techStackRef.value) return

  const observer = new IntersectionObserver(
    (entries) => {
      entries.forEach(entry => {
        if (entry.isIntersecting) {
          const tags = techStackRef.value.querySelectorAll('.tech-tag')
          // 先设置初始状态
          tags.forEach(tag => {
            tag.style.opacity = '0'
            tag.style.transform = 'scale(0.8)'
          })
          techAnimation = animate(tags, {
            opacity: { from: 0, to: 1 },
            scale: { from: 0.8, to: 1 },
            delay: stagger(80),
            duration: 500,
            ease: 'outBack'
          })
          observer.unobserve(entry.target)
        }
      })
    },
    { threshold: 0.5 }
  )

  observer.observe(techStackRef.value)
}

// 初始化流程节点脉冲动画
const initFlowAnimation = () => {
  if (!flowDiagramRef.value) return

  const nodes = flowDiagramRef.value.querySelectorAll('.flow-node')
  const hubNode = flowDiagramRef.value.querySelector('.flow-node--hub')

  // 为所有节点添加轻微的呼吸动画
  nodes.forEach((node, index) => {
    animate(node, {
      scale: { from: 1, to: 1.02 },
      duration: 3000 + index * 500,
      ease: 'inOutSine',
      loop: true,
      alternate: true,
      delay: index * 300
    })
  })

  // 为 hub 节点添加更强的脉冲效果
  if (hubNode) {
    flowAnimation = animate(hubNode, {
      scale: { from: 1, to: 1.05 },
      duration: 2000,
      ease: 'inOutQuad',
      loop: true,
      alternate: true
    })
  }
}

const formatCount = (count) =>
  Number.isFinite(count) && count >= 0 ? count.toLocaleString('en-US') : ''

// 首页统计直接展示实时的 GitHub 仓库数据，不再依赖 branding 配置
const realtimeStats = computed(() => {
  const stats = githubStats.value
  if (!stats) {
    return []
  }

  return [
    { key: 'stars', label: 'Stars', value: formatCount(stats.stars), icon: Star },
    { key: 'forks', label: 'Forks', value: formatCount(stats.forks), icon: GitFork },
    { key: 'issues', label: 'Open Issues', value: formatCount(stats.issues), icon: CircleDot }
  ]
})
</script>

<style lang="less" scoped>
.home-container {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  color: var(--main-900);
  background: var(--main-5);
  position: relative;
  overflow-x: hidden;
}

// 加载中状态
.loading-container {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  gap: 1rem;

  .loading-text {
    color: var(--gray-600);
    font-size: 0.95rem;
  }
}

// 错误状态
.error-container {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  padding: 2rem;
}

// 氛围装饰背景
.ambient {
  position: absolute;
  inset: 0;
  z-index: 0;
  overflow: hidden;
  pointer-events: none;
}

.orb {
  position: absolute;
  border-radius: 50%;
  filter: blur(70px);
  will-change: transform;
}

.orb-1 {
  width: 440px;
  height: 440px;
  top: -140px;
  right: -90px;
  background: var(--main-100);
  opacity: 0.55;
  animation: orbFloat 18s ease-in-out infinite;
}

.orb-2 {
  width: 380px;
  height: 380px;
  bottom: -160px;
  left: -120px;
  background: var(--main-200);
  opacity: 0.4;
  animation: orbFloat 22s ease-in-out infinite reverse;
}

.orb-3 {
  width: 300px;
  height: 300px;
  top: 32%;
  left: 52%;
  background: var(--main-50);
  opacity: 0.6;
  animation: orbFloat 26s ease-in-out infinite;
}

.grid-mesh {
  position: absolute;
  inset: 0;
  background-image:
    linear-gradient(to right, var(--main-40) 1px, transparent 1px),
    linear-gradient(to bottom, var(--main-40) 1px, transparent 1px);
  background-size: 60px 60px;
  opacity: 0.7;
  -webkit-mask-image: radial-gradient(ellipse 75% 55% at 50% 8%, #000, transparent 72%);
  mask-image: radial-gradient(ellipse 75% 55% at 50% 8%, #000, transparent 72%);
}

// 顶部导航
.glass-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  width: 100%;
  padding: 0.85rem 2.5rem;
  background-color: var(--color-trans-light);
  backdrop-filter: blur(20px);
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  z-index: 100;
  border-bottom: 1px solid var(--main-40);
}

.header-actions {
  display: flex;
  align-items: center;
  gap: 0.75rem;
}

.logo {
  display: flex;
  align-items: center;
  font-weight: bold;
  color: var(--main-800);

  .logo-img {
    height: 2rem;
    margin-right: 0.6rem;
  }
}

.logo-text {
  font-size: 1.3rem;
  font-weight: 600;
}

.github-link {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 38px;
  height: 38px;
  border-radius: 10px;
  text-decoration: none;
  color: var(--gray-600);
  border: 1px solid transparent;
  transition:
    color 0.2s ease,
    background 0.2s ease,
    border-color 0.2s ease;

  &:hover {
    color: var(--main-700);
    background: var(--main-30);
    border-color: var(--main-40);
  }

  svg {
    fill: currentColor;
  }
}

// Hero
.hero-section {
  position: relative;
  z-index: 1;
  flex: 1;
  width: 100%;
  display: flex;
  flex-direction: column;
  justify-content: center;
  padding: 7rem 2rem 3rem;
}

.hero-layout {
  display: grid;
  grid-template-columns: 1.05fr 0.95fr;
  gap: 3rem;
  align-items: start;
  width: 100%;
  max-width: 1180px;
  margin: 0 auto;
}

.hero-content {
  display: flex;
  flex-direction: column;
  gap: 1.4rem;
  padding-top: 0.5rem;
}

.reveal-up {
  opacity: 0;
  transform: translateY(16px);
  animation: revealUp 0.7s cubic-bezier(0.22, 1, 0.36, 1) forwards;
}

.reveal-up.delay-1 {
  animation-delay: 110ms;
}

.reveal-up.delay-2 {
  animation-delay: 220ms;
}

.hero-badge {
  display: inline-flex;
  align-items: center;
  gap: 0.5rem;
  align-self: flex-start;
  padding: 0.4rem 0.9rem;
  border-radius: 999px;
  background: var(--main-0);
  border: 1px solid var(--main-40);
  color: var(--main-700);
  font-size: 0.85rem;
  letter-spacing: 0.02em;
  font-weight: 600;
  margin: 0;
  box-shadow: 0 4px 14px -8px rgba(3, 80, 101, 0.4);
}

.badge-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--main-500);
  box-shadow: 0 0 0 4px var(--main-50);
  flex-shrink: 0;
}

.hero-badge-link {
  color: inherit;
  text-decoration: none;
}

.hero-badge-number {
  color: var(--main-700);
  font-weight: 700;
  transition: color 0.2s ease;
}

.hero-badge-link:hover .hero-badge-number {
  color: var(--main-800);
}

.hero-badge.typing::after {
  content: '';
  display: inline-block;
  width: 1px;
  height: 1em;
  margin-left: 2px;
  background: var(--main-600);
  vertical-align: -0.1em;
  animation: caretBlink 0.8s steps(1, end) infinite;
}

.title {
  font-size: clamp(2.6rem, 4.4vw, 4.2rem);
  font-weight: 800;
  margin: 0;
  background: linear-gradient(120deg, var(--main-900) 10%, var(--main-600) 60%, var(--main-500));
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
  letter-spacing: -0.02em;
  line-height: 1.08;
}

.subtitle {
  font-size: 1.45rem;
  font-weight: 600;
  color: var(--gray-700);
  line-height: 1.45;
  margin: 0;
  min-height: calc(1.45em * 1.3);
}

.pain-point {
  font-size: 0.95rem;
  color: var(--gray-600);
  line-height: 1.6;
  margin: 0;
  max-width: 520px;
}

.subtitle-switch-enter-active,
.subtitle-switch-leave-active {
  transition:
    opacity 0.32s ease,
    transform 0.32s ease;
}

.subtitle-switch-enter-from,
.subtitle-switch-leave-to {
  opacity: 0;
  transform: translateY(7px);
}

.hero-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 1.25rem;
  align-items: center;
  margin-top: 0.5rem;
}

.button-base {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 0.5rem;
  padding: 0.5rem 2rem;
  border-radius: 999px;
  font-size: 1.05rem;
  font-weight: 600;
  cursor: pointer;
  border: 1px solid transparent;
  text-decoration: none;
  transition:
    background 0.25s ease,
    box-shadow 0.25s ease;
  min-height: 52px;
}

.button-base.primary {
  background: linear-gradient(135deg, var(--main-600), var(--main-500));
  color: var(--gray-0);
  box-shadow: 0 12px 28px -12px rgba(3, 80, 101, 0.55);
  position: relative;
  overflow: hidden;

  /* 呼吸微光动画 */
  &::before {
    content: '';
    position: absolute;
    top: 0;
    left: -100%;
    width: 100%;
    height: 100%;
    background: linear-gradient(
      90deg,
      transparent,
      rgba(255, 255, 255, 0.2),
      transparent
    );
    animation: shimmer 3s ease-in-out infinite;
  }

  :deep(svg) {
    transition: transform 0.25s ease;
  }

  &:hover {
    background: linear-gradient(135deg, var(--main-700), var(--main-600));
    box-shadow: 0 16px 34px -12px rgba(3, 80, 101, 0.6);
    transform: translateY(-1px);

    :deep(svg) {
      transform: translateX(3px);
    }
  }
}

@keyframes shimmer {
  0% {
    left: -100%;
  }
  50%,
  100% {
    left: 100%;
  }
}

.button-base.secondary {
  background: var(--main-0);
  color: var(--main-700);
  border-color: var(--main-40);
  padding: 0.5rem 1.6rem;

  :deep(svg) {
    color: var(--main-600);
  }

  &:hover {
    background: var(--main-30);
    border-color: var(--main-200);
    color: var(--main-800);
  }
}

// Hero 右侧可视化卡片
.hero-visual {
  display: flex;
  justify-content: center;
}

.visual-card {
  position: relative;
  width: 100%;
  max-width: 460px;
  padding: 1.75rem;
  border-radius: 24px;
  background: linear-gradient(165deg, var(--main-0), var(--main-20));
  border: 1px solid var(--main-40);
  box-shadow: 0 30px 60px -34px rgba(3, 80, 101, 0.35);
  overflow: hidden;
}

.visual-glow {
  position: absolute;
  top: -40%;
  right: -20%;
  width: 70%;
  height: 70%;
  background: radial-gradient(circle, var(--main-100), transparent 70%);
  opacity: 0.7;
  pointer-events: none;
}

.graph-watermark {
  position: absolute;
  top: -26px;
  right: -26px;
  width: 200px;
  height: auto;
  color: var(--main-500);
  opacity: 0.09;
  pointer-events: none;
}

// Harness → RAG 引擎 → 知识库 横向数据流
.flow-diagram {
  position: relative;
  z-index: 1;
}

.flow-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
}

.flow-node {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0.4rem;
  flex-shrink: 0;
  width: 90px;
  text-align: center;
}

.flow-icon {
  width: 54px;
  height: 54px;
  border-radius: 16px;
  background: var(--main-30);
  border: 1px solid var(--main-40);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  transition:
    background 0.2s ease,
    border-color 0.2s ease;

  :deep(svg) {
    color: var(--main-700);
  }
}

.flow-node:hover .flow-icon {
  background: var(--main-100);
  border-color: var(--main-200);
}

.flow-name {
  font-size: 0.8rem;
  font-weight: 600;
  color: var(--main-800);
  line-height: 1.3;
}

.flow-desc {
  font-size: 0.7rem;
  color: var(--gray-600);
  line-height: 1.4;
  opacity: 0.85;
}

// 中间枢纽：主色高亮 + 脉冲环
.flow-icon--hub {
  position: relative;
  width: 60px;
  height: 60px;
  border-radius: 18px;
  background: linear-gradient(140deg, var(--main-500), var(--main-600));
  border: none;
  box-shadow: 0 10px 22px -10px rgba(3, 80, 101, 0.55);

  :deep(svg) {
    color: var(--gray-0);
    position: relative;
    z-index: 1;
  }
}

.flow-node--hub:hover .flow-icon--hub {
  background: linear-gradient(140deg, var(--main-500), var(--main-600));
}

.hub-ring {
  position: absolute;
  inset: 0;
  border-radius: inherit;
  border: 2px solid var(--main-400);
  animation: hubPulse 2.4s ease-out infinite;
}

.flow-link {
  position: relative;
  flex: 1;
  height: 54px;
  min-width: 0;
}

.flow-rail {
  position: absolute;
  left: 4px;
  right: 4px;
  top: 50%;
  height: 2px;
  transform: translateY(-50%);
  border-radius: 2px;
  background: linear-gradient(
    90deg,
    var(--main-50),
    var(--main-200) 25%,
    var(--main-200) 75%,
    var(--main-50)
  );
}

.flow-dot {
  position: absolute;
  width: 8px;
  height: 8px;
  border-radius: 50%;
}

.flow-dot--fwd {
  top: calc(50% - 5px);
  background: var(--main-500);
  box-shadow: 0 0 0 4px var(--main-50);
  animation: flowRight 2.4s linear infinite;
  animation-delay: calc(var(--i) * 1.2s);
}

.flow-dot--back {
  top: calc(50% + 5px);
  transform: translateY(-100%);
  background: var(--main-300);
  box-shadow: 0 0 0 4px var(--main-30);
  animation: flowLeft 2.4s linear infinite;
  animation-delay: calc(var(--i) * 1.2s + 0.6s);
}

.flow-caption {
  margin: 1.25rem 0 0;
  text-align: center;
  font-size: 0.84rem;
  color: var(--gray-600);
  line-height: 1.5;
}

.stat-row {
  position: relative;
  display: flex;
  margin-top: 1.5rem;
  padding-top: 1.35rem;
  border-top: 1px solid var(--main-40);
}

.stat-item {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 0.3rem;

  &:not(:first-child) {
    padding-left: 1.2rem;
  }

  &:not(:last-child) {
    padding-right: 1.2rem;
    border-right: 1px solid var(--main-40);
  }
}

.stat-item-value {
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
  font-size: 1.3rem;
  font-weight: 700;
  color: var(--main-800);
  line-height: 1.1;

  :deep(svg) {
    color: var(--main-500);
  }
}

.stat-item-label {
  font-size: 0.8rem;
  color: var(--gray-600);
}

// 功能亮点卡片区域
.features-section {
  position: relative;
  z-index: 1;
  padding: 4rem 2rem;
  background: var(--main-5);
}

.features-container {
  max-width: 1180px;
  margin: 0 auto;
}

.features-title {
  font-size: 2rem;
  font-weight: 700;
  color: var(--main-900);
  text-align: center;
  margin: 0 0 0.75rem;
  letter-spacing: -0.01em;
}

.features-subtitle {
  font-size: 1rem;
  color: var(--gray-600);
  text-align: center;
  margin: 0 0 3rem;
}

.features-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 1.5rem;
}

.feature-card {
  background: var(--color-bg-elevated);
  border: 1px solid var(--main-40);
  border-radius: 16px;
  padding: 1.75rem;
  transition: all 0.3s ease;
  cursor: default;

  &:hover {
    transform: translateY(-4px);
    box-shadow: 0 12px 32px -8px rgba(3, 80, 101, 0.2);
    border-color: var(--main-200);
  }
}

.feature-icon {
  width: 48px;
  height: 48px;
  border-radius: 12px;
  background: linear-gradient(135deg, var(--main-100), var(--main-50));
  display: flex;
  align-items: center;
  justify-content: center;
  margin-bottom: 1.25rem;
  color: var(--main-600);
}

.feature-title {
  font-size: 1.1rem;
  font-weight: 600;
  color: var(--main-900);
  margin: 0 0 0.75rem;
}

.feature-desc {
  font-size: 0.9rem;
  color: var(--gray-600);
  line-height: 1.6;
  margin: 0;
}

// 快速部署流程区域
.deploy-section {
  position: relative;
  z-index: 1;
  padding: 4rem 2rem;
  background: var(--main-5);
}

.deploy-container {
  max-width: 1180px;
  margin: 0 auto;
}

.deploy-title {
  font-size: 2rem;
  font-weight: 700;
  color: var(--main-900);
  text-align: center;
  margin: 0 0 0.75rem;
  letter-spacing: -0.01em;
}

.deploy-subtitle {
  font-size: 1rem;
  color: var(--gray-600);
  text-align: center;
  margin: 0 0 3rem;
}

.deploy-steps {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 1rem;
  margin-bottom: 3rem;
}

.deploy-step {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 1rem;
}

.step-number {
  width: 40px;
  height: 40px;
  border-radius: 50%;
  background: linear-gradient(135deg, var(--main-600), var(--main-500));
  color: white;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 1.1rem;
  font-weight: 700;
  flex-shrink: 0;
}

.step-content {
  text-align: center;
}

.step-title {
  font-size: 1rem;
  font-weight: 600;
  color: var(--main-900);
  margin: 0 0 0.5rem;
}

.step-code {
  display: inline-block;
  padding: 0.4rem 0.8rem;
  background: var(--main-0);
  border: 1px solid var(--main-40);
  border-radius: 6px;
  font-size: 0.85rem;
  color: var(--main-700);
  font-family: 'Courier New', monospace;
}

.step-arrow {
  color: var(--main-400);
  margin-top: -2rem;
}

.tech-stack {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 1rem;
  flex-wrap: wrap;
}

.tech-label {
  font-size: 0.9rem;
  color: var(--gray-600);
  font-weight: 500;
}

.tech-tags {
  display: flex;
  gap: 0.5rem;
  flex-wrap: wrap;
}

.tech-tag {
  padding: 0.35rem 0.75rem;
  background: var(--main-0);
  border: 1px solid var(--main-40);
  border-radius: 6px;
  font-size: 0.85rem;
  color: var(--main-700);
  font-weight: 500;
}

// 页脚
.footer {
  position: relative;
  z-index: 1;
  margin-top: auto;
  border-top: 1px solid var(--main-40);
  background: var(--main-5);
}

.footer-container {
  max-width: 1180px;
  margin: 0 auto;
  padding: 3rem 2rem 2rem;
}

.footer-top {
  display: flex;
  justify-content: space-between;
  gap: 3rem;
  margin-bottom: 2rem;
}

.footer-brand {
  flex: 0 0 280px;
}

.footer-logo {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  margin-bottom: 0.75rem;
}

.footer-logo-img {
  height: 1.75rem;
}

.footer-logo-text {
  font-size: 1.1rem;
  font-weight: 600;
  color: var(--main-900);
}

.footer-desc {
  font-size: 0.9rem;
  color: var(--gray-600);
  line-height: 1.5;
  margin: 0;
}

.footer-links {
  display: flex;
  gap: 4rem;
}

.footer-column {
  min-width: 140px;
}

.footer-column-title {
  font-size: 0.9rem;
  font-weight: 600;
  color: var(--main-900);
  margin: 0 0 1rem;
}

.footer-link-list {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 0.6rem;
}

.footer-link-list a {
  font-size: 0.9rem;
  color: var(--gray-600);
  text-decoration: none;
  transition: color 0.2s ease;

  &:hover {
    color: var(--main-600);
  }
}

.footer-bottom {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding-top: 1.5rem;
  border-top: 1px solid var(--main-40);
}

.footer-meta {
  display: flex;
  gap: 0.5rem;
}

.footer-badge {
  padding: 0.25rem 0.6rem;
  background: var(--main-0);
  border: 1px solid var(--main-40);
  border-radius: 4px;
  font-size: 0.75rem;
  color: var(--main-700);
  font-weight: 500;
}

.copyright {
  color: var(--main-700);
  font-size: 0.85rem;
  font-weight: 500;
  margin: 0;
  opacity: 0.75;
}

@keyframes revealUp {
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

@keyframes caretBlink {
  50% {
    opacity: 0;
  }
}

@keyframes orbFloat {
  0%,
  100% {
    transform: translate(0, 0) scale(1);
  }
  50% {
    transform: translate(0, -26px) scale(1.04);
  }
}

@keyframes flowRight {
  0% {
    left: -4px;
    opacity: 0;
  }
  15% {
    opacity: 1;
  }
  85% {
    opacity: 1;
  }
  100% {
    left: calc(100% - 4px);
    opacity: 0;
  }
}

@keyframes flowLeft {
  0% {
    left: calc(100% - 4px);
    opacity: 0;
  }
  15% {
    opacity: 1;
  }
  85% {
    opacity: 1;
  }
  100% {
    left: -4px;
    opacity: 0;
  }
}

@keyframes hubPulse {
  0% {
    opacity: 0.6;
    transform: scale(1);
  }
  70%,
  100% {
    opacity: 0;
    transform: scale(1.4);
  }
}

// 暗色模式
:global(:root.dark) {
  .home-container {
    background: var(--main-5);
  }

  .hero-badge-number {
    color: var(--main-200);
  }

  .hero-badge-link:hover .hero-badge-number {
    color: var(--main-100);
  }

  .button-base.secondary {
    color: var(--main-200);

    :deep(svg) {
      color: var(--main-300);
    }

    &:hover {
      color: var(--main-100);
    }
  }

  .github-link {
    color: var(--gray-400);

    &:hover {
      color: var(--main-200);
    }
  }
}

@media (prefers-reduced-motion: reduce) {
  .reveal-up,
  .orb,
  .hero-badge.typing::after {
    animation: none;
  }

  .reveal-up {
    opacity: 1;
    transform: none;
  }

  .flow-dot,
  .hub-ring {
    display: none;
  }

  .subtitle-switch-enter-active,
  .subtitle-switch-leave-active {
    transition: none;
  }
}

@media (max-width: 960px) {
  .hero-layout {
    grid-template-columns: 1fr;
    gap: 2.5rem;
  }

  .hero-content {
    align-items: flex-start;
    text-align: left;
  }

  .visual-card {
    max-width: 520px;
    margin: 0 auto;
  }

  // 功能卡片改为2列
  .features-grid {
    grid-template-columns: repeat(2, 1fr);
    gap: 1.25rem;
  }

  // 部署步骤改为纵向
  .deploy-steps {
    flex-direction: column;
    gap: 1.5rem;
  }

  .step-arrow {
    transform: rotate(90deg);
    margin-top: 0;
  }

  // 页脚改为纵向布局
  .footer-top {
    flex-direction: column;
    gap: 2rem;
  }

  .footer-brand {
    flex: 1;
  }

  .footer-links {
    gap: 2rem;
  }
}

@media (max-width: 768px) {
  .glass-header {
    padding: 0.75rem 1.25rem;
  }

  .logo-text {
    font-size: 1.15rem;
  }

  .hero-section {
    padding: 6rem 1.25rem 2.5rem;
  }

  .title {
    font-size: clamp(2.2rem, 9vw, 3rem);
  }

  .subtitle {
    font-size: 1.2rem;
  }

  .button-base {
    width: 100%;
  }

  // 功能卡片改为1列
  .features-grid {
    grid-template-columns: 1fr;
  }

  // 功能区域和部署区域padding调整
  .features-section,
  .deploy-section {
    padding: 3rem 1.25rem;
  }

  .features-title,
  .deploy-title {
    font-size: 1.6rem;
  }

  // 页脚链接改为单列
  .footer-links {
    flex-direction: column;
    gap: 1.5rem;
  }

  .footer-bottom {
    flex-direction: column;
    gap: 1rem;
    text-align: center;
  }
}

// 深色模式优化
:global(:root.dark) {
  .feature-card {
    background: var(--gray-900);
    border-color: var(--gray-800);

    &:hover {
      border-color: var(--main-600);
      box-shadow: 0 12px 32px -8px rgba(0, 0, 0, 0.5);
    }
  }

  .feature-icon {
    background: linear-gradient(135deg, var(--main-900), var(--main-800));
    color: var(--main-400);
  }

  .step-code,
  .tech-tag {
    background: var(--gray-900);
    border-color: var(--gray-800);
    color: var(--main-300);
  }

  .footer-badge {
    background: var(--gray-900);
    border-color: var(--gray-800);
    color: var(--main-300);
  }

  .pain-point {
    color: var(--gray-500);
  }
}
</style>
