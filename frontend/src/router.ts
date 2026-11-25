import { createRouter, createWebHistory } from 'vue-router'

/**
 * 六个视图各自一个分块（首屏只下当前视图那块），但分块 loader 在这里具名保留，
 * 好让下面的空闲预取与路由表用同一份引用——预取过的模块由 ES module 注册表去重，
 * 不会重复请求，后续导航直接从模块图里取，不再等静态资源。
 */
const views = {
  monitor: () => import('@/views/MonitorView.vue'),
  solar: () => import('@/views/SolarView.vue'),
  recognize: () => import('@/views/RecognizeView.vue'),
  console: () => import('@/views/ConsoleView.vue'),
  annotate: () => import('@/views/AnnotateView.vue'),
  clean: () => import('@/views/CleanView.vue'),
  augment: () => import('@/views/AugmentView.vue'),
  export: () => import('@/views/ExportView.vue'),
  backends: () => import('@/views/BackendsView.vue'),
  training: () => import('@/views/TrainingView.vue'),
}

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/monitor' },
    { path: '/monitor', name: 'monitor', component: views.monitor, meta: { title: '空间天气监测台' } },
    { path: '/solar', name: 'solar', component: views.solar, meta: { title: '太阳科学工作台' } },
    { path: '/recognize', name: 'recognize', component: views.recognize, meta: { title: '图片自动识别' } },
    { path: '/console', name: 'console', component: views.console, meta: { title: '管理台' } },
    { path: '/annotate', name: 'annotate', component: views.annotate, meta: { title: '标注' } },
    { path: '/clean', name: 'clean', component: views.clean, meta: { title: '清洗' } },
    { path: '/augment', name: 'augment', component: views.augment, meta: { title: '增强' } },
    { path: '/export', name: 'export', component: views.export, meta: { title: '导出' } },
    { path: '/backends', name: 'backends', component: views.backends, meta: { title: '后端' } },
    { path: '/training', name: 'training', component: views.training, meta: { title: '本地模型实验' } },
    { path: '/:pathMatch(.*)*', redirect: '/console' },
  ],
})

/**
 * 首屏之后预取其余分块（BUG-012 的根因就在这里）：懒加载分块没取到时，点了导航条
 * 页面要等到分块回来才切 URL——本地离线服务上是毫秒级，但慢盘、冷缓存或首屏并发下
 * 会被读成"界面卡住"，而"仍要离开"这类判据要求导航当次就切过去。
 * 只在第一次导航完成后跑一次，空闲时间片执行，不与首屏请求抢带宽。
 */
type IdleWindow = Window & { requestIdleCallback?: (cb: () => void, opt?: { timeout?: number }) => number }
let prefetched = false

router.afterEach(() => {
  if (prefetched) return
  prefetched = true
  const run = () => { Object.values(views).forEach((load) => { void load() }) }
  const w = window as IdleWindow
  if (typeof w.requestIdleCallback === 'function') w.requestIdleCallback(run, { timeout: 2000 })
  else setTimeout(run, 400)
})

export default router
