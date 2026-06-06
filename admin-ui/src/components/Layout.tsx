import { NavLink, Outlet } from 'react-router-dom'

// The canvas (/pipeline) is the home for all flow config — sources, scorers,
// PPR engine, blend weights, filters and outputs are edited by clicking nodes.
// The sidebar only carries the browse/manage views that aren't a single node;
// the canvas-duplicated pages (PPR config, scores, filters, sources, feed…)
// still have live routes and are reached via the node panels' deep-links.
const NAV = [
  {
    label: 'Manage',
    items: [
      { to: '/scoring/graphs',  label: 'Graphs' },
      { to: '/pipeline/backup', label: 'Backup' },
    ],
  },
  {
    label: 'App',
    items: [
      { to: '/app/radio', label: 'Radio' },
    ],
  },
]

const END_ROUTES = new Set(['/pipeline'])

export default function Layout() {
  return (
    <div className="flex min-h-screen">
      <aside className="flex w-48 flex-shrink-0 flex-col border-r border-border bg-bg-2 px-2 py-4">
        <div className="mb-5 px-2.5">
          <span className="text-[13px] font-semibold text-text">recommenderr</span>
          <span className="ml-1.5 text-[10px] text-text-2">admin</span>
        </div>
        <nav className="flex-1 space-y-4">
          {/* Home — the pipeline canvas, primary entry point */}
          <NavLink
            to="/pipeline"
            end
            className={({ isActive }) => `nav-item font-semibold ${isActive ? 'nav-item-active' : ''}`}
          >
            Pipeline
          </NavLink>

          {NAV.map((section) => (
            <div key={section.label}>
              <div className="nav-section-label">{section.label}</div>
              {section.items.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  end={END_ROUTES.has(item.to)}
                  className={({ isActive }) =>
                    `nav-item ${isActive ? 'nav-item-active' : ''}`
                  }
                >
                  {item.label}
                </NavLink>
              ))}
            </div>
          ))}
        </nav>
      </aside>
      <main className="flex-1 overflow-auto p-5">
        <Outlet />
      </main>
    </div>
  )
}
