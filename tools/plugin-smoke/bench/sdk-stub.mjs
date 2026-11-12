export const ROUTES_AREA = 'routes'
export const STATUSBAR_AREAS = { left: 'statusbar.left', right: 'statusbar.right', center: 'statusbar.center' }
export const Tip = ({ children }) => children
export const haptic = () => {}
export const host = { navigate: () => {}, openExternal: () => {} }
export const Button = ({ children, onClick }) => globalThis.__React.createElement('button', { onClick }, children)
export const EmptyState = ({ children }) => children
export const GlyphSpinner = () => null

// A tiny query client: enough for GraphPage's useQuery (data + refetch + isLoading).
export function useQuery() {
  const React = globalThis.__React
  const [state] = React.useState({ data: globalThis.__graphData || null, error: null, isLoading: false })
  const refetch = React.useCallback(() => {}, [])
  return { ...state, refetch }
}
export function useQueryClient() {
  return { invalidateQueries: () => {} }
}
export default {}
