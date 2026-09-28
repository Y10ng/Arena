import { mount } from '@vue/test-utils'
import { defineComponent, nextTick, ref } from 'vue'
import type { PropType } from 'vue'
import { describe, expect, it, vi } from 'vitest'
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

    await buttons[1]?.trigger('click')

    expect(wrapper.emitted('row-action')).toEqual([[rows[1]]])
  })
})
