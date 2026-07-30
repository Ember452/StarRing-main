/**
 * Anime.js 动画 Composable
 * 封装 anime.js v4 的常用动画方法
 */
import { animate, stagger, createTimeline } from 'animejs'

export function useAnime() {
  return {
    animate,
    stagger,
    createTimeline
  }
}
