/**
 * canvas store 骨架（UI规格 §3.3）：当前 uri、帧参数、Ann 草稿、撤销栈。
 * M1 只承载管理台"去标注"跳转所需的最小状态；M2/M3 画布里程碑再充实交互逻辑。
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'

export interface FrameParams {
  index?: number
  channel?: number
  cursor?: number
  stretch?: string
  colormap?: string
  gamma?: number
  width?: number
  height?: number
  [key: string]: unknown
}

export const useCanvasStore = defineStore('canvas', () => {
  const currentUri = ref('')
  const frameParams = ref<FrameParams>({})
  const draftAnns = ref<unknown[]>([])
  const dirty = ref(false)

  function openItem(uri: string) {
    currentUri.value = uri
  }

  function reset() {
    currentUri.value = ''
    frameParams.value = {}
    draftAnns.value = []
    dirty.value = false
  }

  return { currentUri, frameParams, draftAnns, dirty, openItem, reset }
})
