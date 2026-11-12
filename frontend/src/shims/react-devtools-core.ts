/**
 * Stub for Ink's dev-only devtools dependency.
 *
 * `ink` imports `react-devtools-core` from a module it loads only when
 * `DEV=true` (see ink/build/reconciler.js). The release bundle must not carry
 * that dev dependency: it is not installed for users, and the import is
 * resolved statically by the bundler.
 */

export default {
  connectToDevTools(): void {
    /* devtools are unavailable in the release bundle */
  },
};
