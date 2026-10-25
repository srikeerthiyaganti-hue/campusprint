/**
 * Centralized API configuration for XeroxQueue frontend portals.
 *
 * Supports:
 * - Explicit API base URL via VITE_API_BASE_URL (e.g. 'http://127.0.0.1:8000' or 'https://api.xeroxqueue.edu')
 * - Same-origin production reverse-proxy deployments when VITE_API_BASE_URL is empty ('')
 * - Sensible local development fallback to 'http://127.0.0.1:8000' when unset
 */

const rawEnvUrl = import.meta.env.VITE_API_BASE_URL

export const API_BASE_URL =
  typeof rawEnvUrl === 'string' && rawEnvUrl.trim() !== ''
    ? rawEnvUrl.trim().replace(/\/+$/, '')
    : (import.meta.env.DEV ? 'http://127.0.0.1:8000' : '')

export default API_BASE_URL
