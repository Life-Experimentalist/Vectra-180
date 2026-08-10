/* Applies the stored theme before first paint, so the panel never flashes
 * white at a driver checking footage at night.
 *
 * This lives in its own file rather than an inline <script> because the
 * service sends `script-src 'self'`. Pinning a hash in the CSP instead would
 * mean any whitespace change here silently reintroduces the flash.
 *
 * Loaded synchronously from <head>: it must run before the body renders.
 */

"use strict";

try {
  var stored = localStorage.getItem("vectra-theme");
  if (stored === "light" || stored === "dark") {
    document.documentElement.dataset.theme = stored;
  }
} catch (error) {
  /* private mode: the OS preference still applies via prefers-color-scheme */
}
