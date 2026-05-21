import { NavLink, Outlet, useLocation } from 'react-router-dom'

const NAV = [
  {
    label: 'Discovery',
    items: [
      { to: '/discovery/sources', label: 'Sources' },
      { to: '/discovery/items', label: 'Items' },
      { to: '/discovery/raw', label: 'Raw Query' },
    ],
  },
  {
    label: 'Recommendation',
    items: [
      { to: '/recommendation/config', label: 'Config', disabled: true },
      { to: '/recommendation/scores', label: 'Scores', disabled: true },
      { to: '/recommendation/weight-rules', label: 'Weight Rules', disabled: true },
      { to: '/recommendation/filters', label: 'Filters', disabled: true },
    ],
  },
  {
    label: 'Application',
    items: [
      { to: '/app/feed', label: 'Feed', disabled: true },
      { to: '/app/radio', label: 'Radio', disabled: true },
    ],
  },
]

export default function Layout() {
  return (
    <div className="flex min-h-screen">
      {/* Sidebar */}
      <aside className="flex w-48 flex-shrink-0 flex-col border-r border-border bg-bg-2 px-2 py-4">
        <div className="mb-5 px-2.5">
          <span className="text-[13px] font-semibold text-text">recommenderr</span>
          <span className="ml-1.5 text-[10px] text-text-2">admin</span>
        </div>
        <nav className="flex-1 space-y-4">
          {NAV.map((section) => (
            <div key={section.label}>
              <div className="nav-section-label">{section.label}</div>
              {section.items.map((item) =>
                'disabled' in item && item.disabled ? (
                  <div
                    key={item.to}
                    className="nav-item opacity-35 cursor-not-allowed"
                  >
                    {item.label}
                  </div>
                ) : (
                  <NavLink
                    key={item.to}
                    to={item.to}
                    className={({ isActive }) =>
                      `nav-item ${isActive ? 'nav-item-active' : ''}`
                    }
                  >
                    {item.label}
                  </NavLink>
                )
              )}
            </div>
          ))}
        </nav>
        <div className="px-2.5 text-[10px] text-text-2 opacity-40">
          Phase C — Discovery
        </div>
      </aside>

      {/* Main content */}
      <main className="flex-1 overflow-auto p-5">
        <Outlet />
      </main>
    </div>
  )
}
