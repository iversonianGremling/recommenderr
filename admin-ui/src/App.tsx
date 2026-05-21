import { Navigate, Route, Routes } from 'react-router-dom'
import Layout from './components/Layout'
import DiscoverySources from './pages/DiscoverySources'
import DiscoveryItems from './pages/DiscoveryItems'
import DiscoveryRaw from './pages/DiscoveryRaw'

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Layout />}>
        <Route index element={<Navigate to="/discovery/sources" replace />} />
        <Route path="discovery/sources" element={<DiscoverySources />} />
        <Route path="discovery/items" element={<DiscoveryItems />} />
        <Route path="discovery/raw" element={<DiscoveryRaw />} />
        <Route path="*" element={<Navigate to="/discovery/sources" replace />} />
      </Route>
    </Routes>
  )
}
