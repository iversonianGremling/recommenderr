import { Navigate, Route, Routes } from 'react-router-dom'
import Layout from './components/Layout'
import DiscoverySources from './pages/DiscoverySources'
import DiscoveryItems from './pages/DiscoveryItems'
import DiscoveryRaw from './pages/DiscoveryRaw'
import RecommendationConfig from './pages/RecommendationConfig'
import RecommendationScores from './pages/RecommendationScores'
import RecommendationWeightRules from './pages/RecommendationWeightRules'
import RecommendationFilters from './pages/RecommendationFilters'

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Layout />}>
        <Route index element={<Navigate to="/discovery/sources" replace />} />
        <Route path="discovery/sources" element={<DiscoverySources />} />
        <Route path="discovery/items" element={<DiscoveryItems />} />
        <Route path="discovery/raw" element={<DiscoveryRaw />} />
        <Route path="recommendation/config" element={<RecommendationConfig />} />
        <Route path="recommendation/scores" element={<RecommendationScores />} />
        <Route path="recommendation/weight-rules" element={<RecommendationWeightRules />} />
        <Route path="recommendation/filters" element={<RecommendationFilters />} />
        <Route path="*" element={<Navigate to="/discovery/sources" replace />} />
      </Route>
    </Routes>
  )
}
