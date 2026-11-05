import { mount } from '@vue/test-utils'
import { defineComponent, nextTick, ref } from 'vue'
import type { PropType } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import DataTable from './DataTable.vue'
import type {
  DataTableActiveRowAria,
  DataTableColumn,
  DataTableDensity,
  DataTableRowAttrs,
} from './DataTable.vue'

type ProjectRow = {
  id: string
  name: string
  count: number
}

const rows: readonly ProjectRow[] = [
  { id: 'project-a', name: 'Alpha', count: 2 },
  { id: 'project-b', name: 'Beta', count: 5 },
]

const columns = [
  { key: 'name', label: '项目', value: (row) => row.name },
  { key: 'count', label: '数量', align: 'end', value: (row) => row.count },
] satisfies readonly DataTableColumn<ProjectRow, 'name' | 'count'>[]

const DataTableHost = defineComponent({
  components: { DataTable },
  props: {
    initialRows: {
      type: Array as PropType<readonly ProjectRow[]>,
      required: true,
    },
    loading: Boolean,
    density: {
      type: String as PropType<DataTableDensity>,
      default: 'default',
    },
    active: Boolean,
    activeRowAria: {
      type: String as PropType<DataTableActiveRowAria>,
      default: undefined,
    },
    actions: Boolean,
    withRowAttrs: Boolean,
    withUnsafeRowAttrs: Boolean,
  },
  emits: {
    'row-action': (row: ProjectRow) => Boolean(row.id),
  },
  setup(props, { emit }) {
    const tableRows = ref([...props.initialRows])
    const rowKey = (row: ProjectRow) => row.id
    const isRowActive = (row: ProjectRow) => props.active && row.id === 'project-b'
    const actionLabel = (row: ProjectRow) => `查看 ${row.name}`
    const rowAttrs = (row: ProjectRow, rowIndex: number): DataTableRowAttrs => ({
      'aria-label': `${row.name} 第 ${rowIndex + 1} 行`,
      class: `consumer-row consumer-row--${rowIndex}`,
    })
    const rowRef = vi.fn()
    const unsafeRowAttrs = (row: ProjectRow, rowIndex: number) =>
      ({
        ...rowAttrs(row, rowIndex),
        key: rowIndex,
        ref: rowRef,
      }) as unknown as DataTableRowAttrs
    const reverseRows = () => tableRows.value.reverse()
    const onRowAction = (row: ProjectRow) => emit('row-action', row)

    return {
      actionLabel,
      columns,
      isRowActive,
      onRowAction,
      reverseRows,
      rowAttrs,
      rowKey,
      rowRef,
      unsafeRowAttrs,
      tableRows,
    }
  },
  template: `
    <DataTable
      :rows="tableRows"
      :columns="columns"
      :row-key="rowKey"
      caption="项目成果"
      :density="density"
      :loading="loading"
      loading-label="正在同步项目"
      empty-label="没有项目"
      :is-row-active="isRowActive"
      :active-row-aria="activeRowAria"
      :row-attrs="withUnsafeRowAttrs ? unsafeRowAttrs : withRowAttrs ? rowAttrs : undefined"
      :action-label="actions ? actionLabel : undefined"
      action-column-label="详情"
      @row-action="onRowAction"
    >
      <template #cell-name="{ row }">
        <strong :data-test="\`name-\${row.id}\`" :data-row-id="row.id">{{ row.name }}</strong>
      </template>
    </DataTable>
  `,
})

afterEach(() => vi.unstubAllGlobals())

describe('DataTable', () => {
  it('renders native table semantics, a caption, column slots, and a scroll region', () => {
    const wrapper = mount(DataTableHost, { props: { initialRows: rows } })
    const region = wrapper.get('[role="region"]')
    const table = region.get('table')

    expect(region.classes()).toContain('data-table__scroll')
    expect(region.attributes('aria-label')).toBe('项目成果')
    expect(table.get('caption').text()).toBe('项目成果')
    expect(table.findAll('thead th')).toHaveLength(2)
    expect(
      table.findAll('thead th').every((heading) => heading.attributes('scope') === 'col'),
    ).toBe(true)
    expect(table.findAll('tbody tr')).toHaveLength(2)
    expect(table.findAll('tbody td')).toHaveLength(4)
    expect(wrapper.get('[data-test="name-project-a"]').text()).toBe('Alpha')
    expect(table.findAll('tbody tr')[0]?.findAll('td')[1]?.text()).toBe('2')
  })

  it('uses stable row keys and exposes compact and active states', async () => {
    const wrapper = mount(DataTableHost, {
      props: {
        initialRows: rows,
        density: 'compact',
        active: true,
        withUnsafeRowAttrs: true,
      },
    })
    const originalAlphaRow = wrapper.get('[data-row-id="project-a"]').element.closest('tr')

    expect(wrapper.get('table').classes()).toContain('data-table--compact')
    expect(wrapper.get('[data-row-id="project-b"]').element.closest('tr')?.classList).toContain(
      'data-table__row--active',
    )
    expect(wrapper.vm.rowRef).not.toHaveBeenCalled()

    wrapper.vm.reverseRows()
    await nextTick()

    expect(wrapper.get('[data-row-id="project-a"]').element.closest('tr')).toBe(originalAlphaRow)
    expect(wrapper.vm.rowRef).not.toHaveBeenCalled()
  })

  it('maps active rows to explicit current or selected semantics and merges typed row attrs', () => {
    const currentWrapper = mount(DataTableHost, {
      props: {
        initialRows: rows,
        active: true,
        activeRowAria: 'current',
        withRowAttrs: true,
      },
    })
    const currentRows = currentWrapper.findAll('tbody tr')

    expect(currentRows[0]?.attributes('aria-label')).toBe('Alpha 第 1 行')
    expect(currentRows[0]?.classes()).toContain('consumer-row')
    expect(currentRows[0]?.classes()).toContain('consumer-row--0')
    expect(currentRows[0]?.attributes('aria-current')).toBeUndefined()
    expect(currentRows[1]?.attributes('aria-current')).toBe('true')

    const selectedWrapper = mount(DataTableHost, {
      props: { initialRows: rows, active: true, activeRowAria: 'selected' },
    })
    expect(
      selectedWrapper.findAll('tbody tr').map((row) => row.attributes('aria-selected')),
    ).toEqual(['false', 'true'])
  })

  it('renders loading and empty states outside the table with loading taking priority', async () => {
    const wrapper = mount(DataTableHost, {
      props: { initialRows: [], loading: true },
    })

    expect(wrapper.get('[role="status"]').text()).toBe('正在同步项目')
    expect(wrapper.find('table').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('没有项目')

    await wrapper.setProps({ loading: false })

    expect(wrapper.find('[role="status"]').exists()).toBe(false)
    expect(wrapper.find('table').exists()).toBe(false)
    expect(wrapper.text()).toBe('没有项目')
  })

  it('uses a real labeled action button and emits the exact row', async () => {
    const wrapper = mount(DataTableHost, {
      props: { initialRows: rows, actions: true },
    })
    const buttons = wrapper.findAll('tbody button')

    expect(wrapper.findAll('thead th').at(-1)?.text()).toBe('详情')
    expect(wrapper.get('thead th:last-child').classes()).toContain('data-table__action-heading')
    expect(buttons.map((button) => button.text())).toEqual(['详情', '详情'])
    expect(buttons.map((button) => button.attributes('aria-label'))).toEqual([
      '查看 Alpha',
      '查看 Beta',
    ])
    expect(
      wrapper
        .findAll('tbody td:last-child')
        .every((cell) => cell.classes().includes('data-table__action-cell')),
    ).toBe(true)
    expect(buttons.every((button) => button.attributes('type') === 'button')).toBe(true)
    expect(wrapper.findAll('td.data-table__action-cell > .data-table__actions')).toHaveLength(2)
    expect(
      wrapper.findAll('.data-table__actions').every((group) => group.findAll('button').length === 1),
    ).toBe(true)

    await buttons[1]?.trigger('click')

    expect(wrapper.emitted('row-action')).toEqual([[rows[1]]])
  })
})

describe('DataTable 固定外缘调宽', () => {
  const resizeColumns = [
    { key: 'time', label: '时间', width: 100, minWidth: 80 },
    { key: 'host', label: 'Host', width: 300, minWidth: 120, flexible: true },
    { key: 'path', label: '路径', width: 500, minWidth: 160, flexible: true },
    { key: 'status', label: '状态', width: 100, minWidth: 80 },
  ] satisfies DataTableColumn<ProjectRow>[]
  async function setupTable() {
    let width = 1200
    let notify = () => {}
    const disconnect = vi.fn()
    vi.stubGlobal(
      'ResizeObserver',
      class {
        constructor(callback: () => void) {
          notify = callback
        }
        observe(element: HTMLElement) {
          Object.defineProperty(element, 'clientWidth', { configurable: true, get: () => width })
        }
        disconnect = disconnect
      },
    )
    const select = vi.fn()
    const wrapper = mount(DataTable, {
      props: {
        rows,
        columns: resizeColumns,
        rowKey: (row) => (row as ProjectRow).id,
        caption: '流量',
        resizableColumns: true,
        rowAttrs: () => ({ onClick: select }),
      },
    })
    await nextTick()
    return {
      wrapper,
      select,
      disconnect,
      widths: () =>
        wrapper
          .findAll('col')
          .map((col) => Number.parseFloat((col.element as HTMLElement).style.width)),
      total: () => Number.parseFloat((wrapper.get('table').element as HTMLElement).style.width),
      resize: async (next: number) => {
        width = next
        notify()
        await nextTick()
      },
    }
  }

  it('仅有内部边界，指针和键盘交换相邻列空间并遵守两侧最小宽', async () => {
    const { wrapper, widths, total, select } = await setupTable()
    expect(widths()).toEqual([100, 375, 625, 100])
    expect(total()).toBe(1200)
    expect(wrapper.findAll('[role="separator"]')).toHaveLength(3)
    expect(wrapper.get('th:last-child').find('[role="separator"]').exists()).toBe(false)
    const handle = wrapper.get('[aria-label="调整Host列宽"]')
    expect(handle.attributes('aria-valuemax')).toBe('840')
    const capture = vi.fn()
    const release = vi.fn()
    Object.assign(handle.element, { setPointerCapture: capture, releasePointerCapture: release })
    const pointer = async (type: string, clientX: number, pointerId = 4) => {
      const event = new MouseEvent(type, { bubbles: true, button: 0, clientX })
      Object.defineProperty(event, 'pointerId', { value: pointerId })
      handle.element.dispatchEvent(event)
      await nextTick()
    }
    await pointer('pointerdown', 200)
    await pointer('pointermove', 240, 9)
    expect(widths()).toEqual([100, 375, 625, 100])
    await pointer('pointermove', 240)
    expect(widths()).toEqual([100, 415, 585, 100])
    await pointer('pointerup', 240)
    expect(capture).toHaveBeenCalledWith(4)
    expect(release).toHaveBeenCalledWith(4)
    await handle.trigger('keydown', { key: 'ArrowLeft', shiftKey: true })
    expect(widths()).toEqual([100, 375, 625, 100])
    await handle.trigger('keydown', { key: 'ArrowRight', shiftKey: true })
    expect(widths()).toEqual([100, 415, 585, 100])
    await pointer('pointerdown', 200)
    await pointer('pointermove', 2000)
    expect(widths()).toEqual([100, 840, 160, 100])
    await pointer('pointermove', -2000)
    expect(widths()).toEqual([100, 120, 880, 100])
    await pointer('pointercancel', -2000)
    await pointer('pointermove', 250)
    expect(widths()).toEqual([100, 120, 880, 100])
    await handle.trigger('keydown', { key: 'End' })
    await handle.trigger('keydown', { key: 'ArrowRight' })
    expect(widths()).toEqual([100, 840, 160, 100])
    await handle.trigger('keydown', { key: 'Home' })
    await handle.trigger('keydown', { key: 'ArrowLeft' })
    expect(widths()).toEqual([100, 120, 880, 100])
    expect(total()).toBe(1200)
    await handle.trigger('click')
    expect(select).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('容器改变优先分配弹性列，窄于最小总宽才溢出且恢复用户比例', async () => {
    const { wrapper, widths, total, resize, disconnect } = await setupTable()
    await wrapper
      .get('[aria-label="调整Host列宽"]')
      .trigger('keydown', { key: 'ArrowRight', shiftKey: true })
    await resize(1600)
    expect(widths()).toEqual([100, 581, 819, 100])
    expect(total()).toBe(1600)
    await resize(480)
    expect(widths()).toEqual([100, 120, 160, 100])
    await resize(460)
    expect(widths()).toEqual([90, 120, 160, 90])
    expect(total()).toBe(460)
    await resize(300)
    expect(widths()).toEqual([80, 120, 160, 80])
    expect(total()).toBe(440)
    await resize(1600)
    expect(widths()).toEqual([100, 581, 819, 100])
    await wrapper.setProps({ rows: [...rows].reverse() })
    expect(widths()).toEqual([100, 581, 819, 100])
    await wrapper.setProps({ columns: resizeColumns.slice(1) })
    expect(total()).toBe(1600)
    expect(widths()[0]).toBeCloseTo(622.5)
    expect(widths()[1]).toBeCloseTo(877.5)
    expect(wrapper.findAll('[role="separator"]')).toHaveLength(2)
    await wrapper.setProps({ columns: resizeColumns })
    expect(widths()).toEqual([100, 581, 819, 100])
    const region = wrapper.get('[role="region"]')
    const table = wrapper.get('table').element
    const rowElements = wrapper.findAll('tbody tr').map((row) => row.element)
    const handle = wrapper.get<HTMLElement>('[aria-label="调整Host列宽"]')
    document.body.appendChild(region.element)
    handle.element.focus()
    try {
      expect(document.activeElement).toBe(handle.element)
      await wrapper.setProps({ loading: true })
      expect(region.attributes('aria-busy')).toBe('true')
      expect(wrapper.get('table').element).toBe(table)
      wrapper.findAll('tbody tr').forEach((row, index) => {
        expect(row.element).toBe(rowElements[index])
      })
      expect(document.activeElement).toBe(handle.element)
      expect(widths()).toEqual([100, 581, 819, 100])
      expect(disconnect).not.toHaveBeenCalled()
      await wrapper.setProps({ loading: false })
      expect(region.attributes('aria-busy')).toBeUndefined()
      expect(document.activeElement).toBe(handle.element)
      expect(widths()).toEqual([100, 581, 819, 100])
    } finally {
      wrapper.unmount()
      region.element.remove()
    }
    expect(disconnect).toHaveBeenCalledTimes(1)
  })

  it('未启用调宽时不测量或注入布局，保留普通表格行为', async () => {
    const observer = vi.fn()
    vi.stubGlobal('ResizeObserver', observer)
    const wrapper = mount(DataTableHost, { props: { initialRows: rows, actions: true } })
    await nextTick()
    expect(observer).not.toHaveBeenCalled()
    expect(wrapper.find('colgroup').exists()).toBe(false)
    expect(wrapper.find('[role="separator"]').exists()).toBe(false)
    expect(wrapper.get('table').attributes('style')).toBeUndefined()
    wrapper.unmount()
  })
})
