export const ROUTES_AREA = 'routes'
export const STATUSBAR_AREAS = { left: 'statusbar.left', right: 'statusbar.right', center: 'statusbar.center' }
export const Tip = ({ children }) => children
export const haptic = () => {}
export const host = { navigate: () => {}, openExternal: () => {} }
export const useQuery = () => ({ data: globalThis.__graphData || null, error: null, isLoading: false, refetch: () => {} })
export const useQueryClient = () => ({ invalidateQueries: () => {} })
export const Button = ({ children }) => children
export const EmptyState = ({ children }) => children
export const GlyphSpinner = () => null
export default {}
