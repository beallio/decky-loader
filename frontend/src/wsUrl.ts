// Runs before window.DFL exists; imported only by src/wsrouter.ts so Rollup keeps it in the wsrouter chunk.
export function createClientId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

export function buildWsUrl(authToken: string, clientId: string): string {
  return `ws://127.0.0.1:1337/ws?auth=${encodeURIComponent(authToken)}&client_id=${encodeURIComponent(clientId)}`;
}
