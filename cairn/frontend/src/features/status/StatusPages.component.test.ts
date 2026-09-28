import { mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'
import ForbiddenPage from './ForbiddenPage.vue'
import NotFoundPage from './NotFoundPage.vue'

const mocks = vi.hoisted(() => ({
  replace: vi.fn(),
}))

vi.mock('vue-router', () => ({
  useRouter: () => ({ replace: mocks.replace }),
}))

afterEach(() => {
  mocks.replace.mockReset()
})

describe('status pages', () => {
  it.each([
    {
      component: ForbiddenPage,
      code: '403',
      title: '无权访问此页面',
      description: '当前账户没有访问此页面所需的权限。',
    },
    {
      component: NotFoundPage,
      code: '404',
      title: '页面不存在',
      description: '该地址不存在、已移动或不再可用。',
    },
  ])(
    'renders $code with an accessible return action',
    async ({ component, code, title, description }) => {
      const wrapper = mount(component)

      expect(wrapper.get('h2').text()).toBe(title)
      expect(wrapper.text()).toContain(code)
      expect(wrapper.text()).toContain(description)
      expect(wrapper.find('[role="alert"]').exists()).toBe(false)
      expect(wrapper.text()).not.toContain('重试')

      await wrapper.get('button').trigger('click')
      expect(mocks.replace).toHaveBeenCalledOnce()
      expect(mocks.replace).toHaveBeenCalledWith({ name: 'Overview' })
    },
  )
})
