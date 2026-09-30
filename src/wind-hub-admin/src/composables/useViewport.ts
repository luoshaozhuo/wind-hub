import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

export const VIEWPORT = {
  mobileMax: 767,
  tabletMax: 1199,
} as const

export function useViewport() {
  const width = ref(typeof window === 'undefined' ? 1200 : window.innerWidth)

  const update = () => {
    width.value = window.innerWidth
  }

  onMounted(() => {
    update()
    window.addEventListener('resize', update, { passive: true })
  })

  onBeforeUnmount(() => {
    window.removeEventListener('resize', update)
  })

  const isMobile = computed(() => width.value <= VIEWPORT.mobileMax)
  const isTablet = computed(() => width.value > VIEWPORT.mobileMax && width.value <= VIEWPORT.tabletMax)
  const isDesktop = computed(() => width.value > VIEWPORT.tabletMax)

  return { width, isMobile, isTablet, isDesktop }
}
