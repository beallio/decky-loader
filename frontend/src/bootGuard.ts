// Runs before window.DFL exists; imported only by src/index.ts so Rollup keeps it inside index.js.
export interface FrontendBootHost {
  __deckyFrontendBooted?: boolean;
}

export function claimFrontendBoot(host: FrontendBootHost): boolean {
  if (host.__deckyFrontendBooted) {
    return false;
  }
  host.__deckyFrontendBooted = true;
  return true;
}
