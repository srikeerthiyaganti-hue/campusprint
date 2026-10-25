/**
 * Safe localStorage wrapper for XeroxQueue portal session persistence.
 *
 * Security Note:
 * - Uses localStorage with role-specific keys to ensure state isolation.
 * - Stored user objects are treated as unvalidated UI cache only.
 * - The FastAPI backend verifies cryptographic JWT signatures on every request.
 * - Client-side localStorage does not protect against XSS like HttpOnly cookies.
 */

export const STORAGE_KEYS = {
  STUDENT_TOKEN: 'xq_student_token',
  STUDENT_USER: 'xq_student_user',
  ADMIN_TOKEN: 'xq_admin_token',
  ADMIN_USER: 'xq_admin_user',
  PARTNER_TOKEN: 'xq_partner_token',
  PARTNER_USER: 'xq_partner_user',
  PORTAL_VIEW: 'xq_portal_view', // UI view preference only, never auth proof
}

export function getStoredItem(key) {
  try {
    return localStorage.getItem(key) || ''
  } catch {
    return ''
  }
}

export function setStoredItem(key, value) {
  try {
    localStorage.setItem(key, value)
  } catch {
    // Gracefully ignore storage quota or private-mode errors
  }
}

export function removeStoredItem(key) {
  try {
    localStorage.removeItem(key)
  } catch {
    // Gracefully ignore errors
  }
}

export function getStoredJson(key) {
  try {
    const raw = localStorage.getItem(key)
    if (!raw) return null
    return JSON.parse(raw)
  } catch {
    return null
  }
}

export function setStoredJson(key, data) {
  try {
    localStorage.setItem(key, JSON.stringify(data))
  } catch {
    // Gracefully ignore storage quota or serialization errors
  }
}

export function clearRoleSession(role) {
  if (role === 'student') {
    removeStoredItem(STORAGE_KEYS.STUDENT_TOKEN)
    removeStoredItem(STORAGE_KEYS.STUDENT_USER)
  } else if (role === 'admin') {
    removeStoredItem(STORAGE_KEYS.ADMIN_TOKEN)
    removeStoredItem(STORAGE_KEYS.ADMIN_USER)
  } else if (role === 'partner') {
    removeStoredItem(STORAGE_KEYS.PARTNER_TOKEN)
    removeStoredItem(STORAGE_KEYS.PARTNER_USER)
  }
}

export function clearConflictingSessions(activeRole) {
  if (activeRole === 'student') {
    clearRoleSession('admin')
    clearRoleSession('partner')
  } else if (activeRole === 'admin') {
    clearRoleSession('student')
    clearRoleSession('partner')
  } else if (activeRole === 'partner') {
    clearRoleSession('student')
    clearRoleSession('admin')
  }
}
