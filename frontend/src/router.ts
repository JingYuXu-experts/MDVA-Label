import { createRouter, createWebHistory } from 'vue-router'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/console' },
    { path: '/console', name: 'console', component: () => import('@/views/ConsoleView.vue'), meta: { title: '管理台' } },
    { path: '/annotate', name: 'annotate', component: () => import('@/views/AnnotateView.vue'), meta: { title: '标注' } },
    { path: '/clean', name: 'clean', component: () => import('@/views/CleanView.vue'), meta: { title: '清洗' } },
    { path: '/augment', name: 'augment', component: () => import('@/views/AugmentView.vue'), meta: { title: '增强' } },
    { path: '/export', name: 'export', component: () => import('@/views/ExportView.vue'), meta: { title: '导出' } },
    { path: '/backends', name: 'backends', component: () => import('@/views/BackendsView.vue'), meta: { title: '后端' } },
    { path: '/:pathMatch(.*)*', redirect: '/console' },
  ],
})

export default router
