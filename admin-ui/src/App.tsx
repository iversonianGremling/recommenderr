import { Navigate, Route, Routes } from 'react-router-dom'
import Layout from './components/Layout'
import DiscoverySources from './pages/DiscoverySources'
import DiscoveryItems from './pages/DiscoveryItems'
import DiscoveryRaw from './pages/DiscoveryRaw'
import RecommendationConfig from './pages/RecommendationConfig'
import RecommendationScores from './pages/RecommendationScores'
import RecommendationCosine from './pages/RecommendationCosine'
import RecommendationWeightRules from './pages/RecommendationWeightRules'
import RecommendationFilters from './pages/RecommendationFilters'
import RecommendationGraph from './pages/RecommendationGraph'
import PipelineCanvas from './pages/PipelineCanvas'
import PipelineConfig from './pages/PipelineConfig'
import ConfigBackup from './pages/ConfigBackup'
import CustomModules from './pages/CustomModules'
import CustomModuleEdit from './pages/CustomModuleEdit'
import PersonasList from './pages/PersonasList'
import PersonaEdit from './pages/PersonaEdit'
import PersonaScores from './pages/PersonaScores'
import AppFeed from './pages/AppFeed'
import AppRadio from './pages/AppRadio'
import AppLibraryRecs from './pages/AppLibraryRecs'
import Graphs from './pages/Graphs'
import IngestionConverters from './pages/IngestionConverters'

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Layout />}>
        <Route index element={<Navigate to="/pipeline" replace />} />

        {/* Pipeline */}
        <Route path="pipeline" element={<PipelineCanvas />} />
        <Route path="pipeline/config" element={<PipelineConfig />} />
        <Route path="pipeline/backup" element={<ConfigBackup />} />
        <Route path="modules" element={<CustomModules />} />
        <Route path="modules/:id" element={<CustomModuleEdit />} />

        {/* Ingestion (was /discovery/*) */}
        <Route path="ingestion/sources" element={<DiscoverySources />} />
        <Route path="ingestion/converters" element={<IngestionConverters />} />
        <Route path="ingestion/items" element={<DiscoveryItems />} />
        <Route path="ingestion/raw" element={<DiscoveryRaw />} />

        {/* Scoring (was /recommendation/*) */}
        <Route path="scoring/graphs" element={<Graphs />} />
        <Route path="scoring/graph" element={<RecommendationGraph />} />
        <Route path="scoring/ppr" element={<RecommendationConfig />} />
        <Route path="scoring/scores" element={<RecommendationScores />} />
        <Route path="scoring/cosine" element={<RecommendationCosine />} />
        <Route path="scoring/personas" element={<PersonasList />} />
        <Route path="personas/:id/edit" element={<PersonaEdit />} />
        <Route path="personas/:id/scores" element={<PersonaScores />} />

        {/* Output (was /recommendation/weight-rules and /recommendation/filters) */}
        <Route path="output/weight-rules" element={<RecommendationWeightRules />} />
        <Route path="output/filters" element={<RecommendationFilters />} />

        {/* Application */}
        <Route path="app/feed" element={<AppFeed />} />
        <Route path="app/radio" element={<AppRadio />} />
        <Route path="app/library" element={<AppLibraryRecs />} />

        {/* Legacy redirects so old bookmarks still work */}
        <Route path="discovery/sources" element={<Navigate to="/ingestion/sources" replace />} />
        <Route path="discovery/items" element={<Navigate to="/ingestion/items" replace />} />
        <Route path="discovery/raw" element={<Navigate to="/ingestion/raw" replace />} />
        <Route path="recommendation/config" element={<Navigate to="/scoring/ppr" replace />} />
        <Route path="recommendation/scores" element={<Navigate to="/scoring/scores" replace />} />
        <Route path="recommendation/cosine" element={<Navigate to="/scoring/cosine" replace />} />
        <Route path="recommendation/weight-rules" element={<Navigate to="/output/weight-rules" replace />} />
        <Route path="recommendation/filters" element={<Navigate to="/output/filters" replace />} />
        <Route path="recommendation/graph" element={<Navigate to="/scoring/graph" replace />} />
        <Route path="recommendation/graphs" element={<Navigate to="/scoring/graphs" replace />} />
        <Route path="personas" element={<Navigate to="/scoring/personas" replace />} />

        <Route path="*" element={<Navigate to="/pipeline" replace />} />
      </Route>
    </Routes>
  )
}
