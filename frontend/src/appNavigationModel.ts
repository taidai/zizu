export type TabletPage = 'workbench' | 'monitor' | 'tree' | 'alarms' | 'strategies' | 'admin' | 'controls'
export type TabletArea = 'runtime' | 'engineering'
type Role = 'admin' | 'engineer' | 'operator'

export function resolveTabletPage(role: Role, page: TabletPage): TabletPage {
  if (page === 'admin' && role !== 'admin') return 'workbench'
  if ((page === 'tree' || page === 'strategies') && role === 'operator') return 'workbench'
  return page
}

export function pagesForArea(role: Role, area: TabletArea): TabletPage[] {
  if (area === 'engineering') {
    if (role === 'operator') return []
    return role === 'admin' ? ['tree', 'alarms', 'strategies', 'admin'] : ['tree', 'alarms', 'strategies']
  }
  return ['workbench', 'monitor', 'controls']
}
