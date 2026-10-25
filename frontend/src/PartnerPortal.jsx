import { useState, useEffect, useCallback } from 'react'
import './PartnerPortal.css'
import { API_BASE_URL as API } from './config'
import {
  STORAGE_KEYS,
  getStoredItem,
  setStoredItem,
  getStoredJson,
  setStoredJson,
  clearRoleSession,
  clearConflictingSessions,
} from './storage'

function PartnerBrand() {
  return (
    <div className="partner-brand">
      <div className="partner-brand-icon">
        <span>Q</span>
        <span className="partner-brand-dot" />
      </div>
      <div>
        <strong>XeroxQueue</strong>
        <small>CAMPUS DELIVERY PARTNER</small>
      </div>
    </div>
  )
}

function normalizeStudentId(id) {
  return String(id || '').trim().toLowerCase()
}

function deriveCollegeEmail(id) {
  const normalized = normalizeStudentId(id)
  return normalized ? `vu.${normalized}@gmail.com` : 'vu.<student_id>@gmail.com'
}

function getErrorMessage(data, fallback) {
  if (typeof data?.detail === 'string') return data.detail
  if (typeof data?.detail?.message === 'string') return data.detail.message
  if (Array.isArray(data?.detail)) {
    return data.detail.map((item) => item.msg || 'Invalid input').join('. ')
  }
  return fallback
}

export default function PartnerPortal({ onBack }) {
  const [authMode, setAuthMode] = useState('login') // 'login' or 'signup'
  const [token, setToken] = useState(() => getStoredItem(STORAGE_KEYS.PARTNER_TOKEN))
  const [partner, setPartner] = useState(() => getStoredJson(STORAGE_KEYS.PARTNER_USER))
  const [loading, setLoading] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [jobs, setJobs] = useState([])
  const [jobsLoading, setJobsLoading] = useState(false)
  const [jobsNotice, setJobsNotice] = useState('')

  // Form states
  const [fullName, setFullName] = useState('')
  const [studentId, setStudentId] = useState('')
  const [phoneNumber, setPhoneNumber] = useState('')
  const [password, setPassword] = useState('')
  const [showPassword, setShowPassword] = useState(false)

  // Feedback states
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')
  const [migrationNotice, setMigrationNotice] = useState('')
  const [actionLoadingId, setActionLoadingId] = useState(null)
  const [actionError, setActionError] = useState('')
  const [actionSuccess, setActionSuccess] = useState('')

  // Check profile for status updates
  const refreshProfile = useCallback(async (authToken) => {
    const activeToken = authToken || token
    if (!activeToken) return

    setRefreshing(true)
    setError('')
    try {
      const response = await fetch(`${API}/api/partner/profile`, {
        headers: { Authorization: `Bearer ${activeToken}` },
      })
      const data = await response.json().catch(() => ({}))

      if (response.status === 503 && data?.detail?.schema_status === 'pending_migration_001') {
        setMigrationNotice(data.detail.message || 'Database migration 001 is pending.')
        return
      }

      if (response.status === 401) {
        clearRoleSession('partner')
        setToken('')
        setPartner(null)
        setJobs([])
        setError('Your partner session has expired. Please sign in again.')
        return
      }

      if (!response.ok) {
        throw new Error(getErrorMessage(data, 'Could not refresh partner profile.'))
      }

      if (data.partner) {
        setPartner(data.partner)
        setStoredJson(STORAGE_KEYS.PARTNER_USER, data.partner)
      }
    } catch (err) {
      setError(err.message || 'Unable to update application status.')
    } finally {
      setRefreshing(false)
    }
  }, [token])

  // Load jobs if partner is approved
  const loadJobs = useCallback(async (authToken) => {
    const activeToken = authToken || token
    if (!activeToken) return

    setJobsLoading(true)
    setJobsNotice('')
    try {
      const response = await fetch(`${API}/api/partner/jobs`, {
        headers: { Authorization: `Bearer ${activeToken}` },
      })
      const data = await response.json().catch(() => ({}))

      if (response.status === 401) {
        clearRoleSession('partner')
        setToken('')
        setPartner(null)
        setJobs([])
        setError('Your partner session has expired. Please sign in again.')
        return
      }

      if (response.status === 503) {
        setJobsNotice('Delivery job system requires database migration 001.')
        return
      }

      if (response.status === 403) {
        setJobsNotice('Delivery jobs are only accessible to approved partners.')
        return
      }

      if (!response.ok) {
        throw new Error(getErrorMessage(data, 'Could not load delivery jobs.'))
      }

      setJobs(Array.isArray(data.jobs) ? data.jobs : [])
    } catch (err) {
      setJobsNotice(err.message || 'Could not load available jobs.')
    } finally {
      setJobsLoading(false)
    }
  }, [token])

  useEffect(() => {
    let active = true
    if (token && partner?.approval_status === 'approved') {
      const fetchJobs = async () => {
        try {
          const res = await fetch(`${API}/api/partner/jobs`, {
            headers: { Authorization: `Bearer ${token}` },
          })
          if (res.ok) {
            const data = await res.json()
            if (active && Array.isArray(data?.jobs)) {
              setJobs(data.jobs)
            }
          }
        } catch {
          // ignore background fetch error
        }
      }

      fetchJobs()
      const interval = setInterval(() => {
        if (active) fetchJobs()
      }, 10000)

      return () => {
        active = false
        clearInterval(interval)
      }
    }
  }, [token, partner?.approval_status])

  async function handleSignup(e) {
    e.preventDefault()
    setLoading(true)
    setError('')
    setSuccess('')
    setMigrationNotice('')

    try {
      const payload = {
        full_name: fullName.trim(),
        student_id: studentId.trim(),
        phone_number: phoneNumber.trim(),
        password,
      }

      const response = await fetch(`${API}/api/partner/auth/signup`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })

      const data = await response.json().catch(() => ({}))

      if (response.status === 503) {
        const msg = data?.detail?.message || getErrorMessage(data, 'Database migration required.')
        setMigrationNotice(msg)
        return
      }

      if (!response.ok) {
        throw new Error(getErrorMessage(data, 'Registration failed.'))
      }

      setSuccess('Application submitted! Your account is pending admin approval. You can sign in to view status.')
      setAuthMode('login')
      setPassword('')
    } catch (err) {
      setError(err.message || 'Registration failed. Please try again.')
    } finally {
      setLoading(false)
    }
  }

  async function handleLogin(e) {
    e.preventDefault()
    setLoading(true)
    setError('')
    setSuccess('')
    setMigrationNotice('')

    try {
      const payload = {
        identifier: studentId.trim(),
        password,
      }

      const response = await fetch(`${API}/api/partner/auth/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })

      const data = await response.json().catch(() => ({}))

      if (response.status === 503) {
        const msg = data?.detail?.message || getErrorMessage(data, 'Database migration required.')
        setMigrationNotice(msg)
        return
      }

      if (!response.ok) {
        throw new Error(getErrorMessage(data, 'Login failed. Check your credentials.'))
      }

      if (!data.access_token || !data.partner) {
        throw new Error('Server returned an incomplete login response.')
      }

      const partnerUser = data.partner
      clearConflictingSessions('partner')
      setToken(data.access_token)
      setPartner(partnerUser)
      setStoredItem(STORAGE_KEYS.PARTNER_TOKEN, data.access_token)
      setStoredJson(STORAGE_KEYS.PARTNER_USER, partnerUser)
      setPassword('')
    } catch (err) {
      setError(err.message || 'Sign in failed.')
    } finally {
      setLoading(false)
    }
  }

  function handleSignOut() {
    clearRoleSession('partner')
    setToken('')
    setPartner(null)
    setJobs([])
    setError('')
    setSuccess('')
    setMigrationNotice('')
    setActionError('')
    setActionSuccess('')
    setActionLoadingId(null)
  }

  const handleAcceptJob = async (jobId) => {
    if (actionLoadingId) return
    setActionLoadingId(jobId)
    setActionError('')
    setActionSuccess('')

    try {
      const response = await fetch(`${API}/api/partner/jobs/${jobId}/accept`, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${token}`,
        },
      })
      const data = await response.json().catch(() => ({}))

      if (response.status === 401) {
        handleSignOut()
        setError('Your partner session has expired. Please sign in again.')
        return
      }

      if (response.status === 409) {
        await loadJobs(token)
        throw new Error(
          getErrorMessage(
            data,
            'This delivery job is no longer available. Another partner may have already claimed it or its state has changed.'
          )
        )
      }

      if (response.status === 404) {
        await loadJobs(token)
        throw new Error(getErrorMessage(data, 'Delivery job was not found.'))
      }

      if (response.status === 403) {
        throw new Error(getErrorMessage(data, 'You do not have permission to accept this delivery job.'))
      }

      if (!response.ok) {
        throw new Error(getErrorMessage(data, 'Failed to accept delivery job.'))
      }

      setActionSuccess(data.message || `Delivery job #${jobId} successfully accepted! It is now in your active deliveries.`)
      await Promise.all([loadJobs(token), refreshProfile(token)])
    } catch (err) {
      setActionError(err.message || 'Unable to accept job.')
    } finally {
      setActionLoadingId(null)
    }
  }

  const handlePickupJob = async (jobId) => {
    if (actionLoadingId) return
    setActionLoadingId(jobId)
    setActionError('')
    setActionSuccess('')

    try {
      const response = await fetch(`${API}/api/partner/jobs/${jobId}/pickup`, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${token}`,
        },
      })
      const data = await response.json().catch(() => ({}))

      if (response.status === 401) {
        handleSignOut()
        setError('Your partner session has expired. Please sign in again.')
        return
      }

      if (response.status === 409) {
        await loadJobs(token)
        throw new Error(
          getErrorMessage(
            data,
            'Job status has changed. Please review the updated job details.'
          )
        )
      }

      if (response.status === 404) {
        await loadJobs(token)
        throw new Error(getErrorMessage(data, 'Delivery job was not found.'))
      }

      if (response.status === 403) {
        throw new Error(getErrorMessage(data, 'You are not the assigned delivery partner for this job.'))
      }

      if (!response.ok) {
        throw new Error(getErrorMessage(data, 'Failed to confirm pickup.'))
      }

      setActionSuccess(data.message || `Delivery job #${jobId} marked as picked up from the print shop.`)
      await Promise.all([loadJobs(token), refreshProfile(token)])
    } catch (err) {
      setActionError(err.message || 'Unable to mark job as picked up.')
    } finally {
      setActionLoadingId(null)
    }
  }

  const handleDeliverJob = async (jobId) => {
    if (actionLoadingId) return
    setActionLoadingId(jobId)
    setActionError('')
    setActionSuccess('')

    try {
      const response = await fetch(`${API}/api/partner/jobs/${jobId}/deliver`, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${token}`,
        },
      })
      const data = await response.json().catch(() => ({}))

      if (response.status === 401) {
        handleSignOut()
        setError('Your partner session has expired. Please sign in again.')
        return
      }

      if (response.status === 409) {
        await loadJobs(token)
        throw new Error(
          getErrorMessage(
            data,
            'Delivery confirmation failed. The job state may have already changed.'
          )
        )
      }

      if (response.status === 404) {
        await loadJobs(token)
        throw new Error(getErrorMessage(data, 'Delivery job was not found.'))
      }

      if (response.status === 403) {
        throw new Error(getErrorMessage(data, 'You are not the assigned delivery partner for this job.'))
      }

      if (!response.ok) {
        throw new Error(getErrorMessage(data, 'Failed to confirm delivery.'))
      }

      setActionSuccess(data.message || `Delivery confirmed! Your completed deliveries count has been updated.`)
      await Promise.all([loadJobs(token), refreshProfile(token)])
    } catch (err) {
      setActionError(err.message || 'Unable to confirm delivery.')
    } finally {
      setActionLoadingId(null)
    }
  }

  // --- LOGGED-IN SCREENS ---
  if (token && partner) {
    const isApproved = partner.approval_status === 'approved'
    const isPending = partner.approval_status === 'pending'
    const isRejected = partner.approval_status === 'rejected'

    return (
      <div className="partner-app-shell">
        <header className="partner-app-header">
          <PartnerBrand />
          <div className="partner-header-right">
            <div className="partner-user-badge">
              <strong>{partner.full_name}</strong>
              <small>ID: {partner.student_id}</small>
            </div>
            <button type="button" className="partner-btn-secondary" onClick={() => refreshProfile(token)} disabled={refreshing}>
              {refreshing ? '↻ Checking...' : '↻ Check Status'}
            </button>
            <button type="button" className="partner-btn-ghost" onClick={handleSignOut}>
              Sign out
            </button>
            <button type="button" className="partner-btn-back" onClick={onBack}>
              ← Student Portal
            </button>
          </div>
        </header>

        <main className="partner-app-main">
          {error && <div className="partner-alert partner-alert-error">{error}</div>}
          {migrationNotice && <div className="partner-alert partner-alert-warning">{migrationNotice}</div>}

          {/* PENDING APPROVAL SCREEN */}
          {isPending && (
            <div className="partner-status-card pending-card">
              <div className="partner-status-badge badge-pending">
                <span className="dot dot-amber" /> Pending Administrator Approval
              </div>
              <h2>Application Under Review</h2>
              <p className="partner-card-intro">
                Thank you for applying to be a XeroxQueue campus delivery partner! Your application has been received and is awaiting vetting by the print shop administrator.
              </p>

              <div className="partner-details-grid">
                <div>
                  <label>Full Name</label>
                  <strong>{partner.full_name}</strong>
                </div>
                <div>
                  <label>Student ID</label>
                  <strong>{partner.student_id}</strong>
                </div>
                <div>
                  <label>College Email</label>
                  <strong>{partner.email}</strong>
                </div>
                <div>
                  <label>Phone Number</label>
                  <strong>{partner.phone_number}</strong>
                </div>
                <div>
                  <label>Application Status</label>
                  <strong className="text-amber">Pending Verification</strong>
                </div>
                <div>
                  <label>Access Permissions</label>
                  <span>Delivery jobs will unlock immediately upon admin approval.</span>
                </div>
              </div>

              <div className="partner-notice-box">
                <span className="notice-icon">ℹ</span>
                <p>
                  Per campus policy, delivery partners must have active student enrollment verified before handling orders. Once approved, you can claim orders and earn delivery fees.
                </p>
              </div>

              <div className="partner-actions-row">
                <button type="button" className="partner-btn-primary" onClick={() => refreshProfile(token)} disabled={refreshing}>
                  {refreshing ? 'Checking...' : '↻ Re-check Approval Status'}
                </button>
                <button type="button" className="partner-btn-secondary" onClick={handleSignOut}>
                  Sign out
                </button>
              </div>
            </div>
          )}

          {/* REJECTED APPLICATION SCREEN */}
          {isRejected && (
            <div className="partner-status-card rejected-card">
              <div className="partner-status-badge badge-rejected">
                <span className="dot dot-red" /> Application Not Approved / Access Revoked
              </div>
              <h2>Application Status Notice</h2>
              <p className="partner-card-intro">
                Your delivery partner application was not approved or partner privileges were revoked by the print shop administration.
              </p>

              {partner.rejection_reason && (
                <div className="partner-reason-box">
                  <strong>Reason provided:</strong>
                  <p>{partner.rejection_reason}</p>
                </div>
              )}

              <div className="partner-details-grid">
                <div>
                  <label>Applicant Name</label>
                  <strong>{partner.full_name}</strong>
                </div>
                <div>
                  <label>Student ID</label>
                  <strong>{partner.student_id}</strong>
                </div>
                <div>
                  <label>College Email</label>
                  <strong>{partner.email}</strong>
                </div>
                <div>
                  <label>Status</label>
                  <strong className="text-red">Rejected / Inactive</strong>
                </div>
              </div>

              <div className="partner-notice-box">
                <span className="notice-icon">✕</span>
                <p>
                  If you believe this is in error, please contact the campus print shop administrator in person with your valid student ID card.
                </p>
              </div>

              <div className="partner-actions-row">
                <button type="button" className="partner-btn-secondary" onClick={() => refreshProfile(token)} disabled={refreshing}>
                  {refreshing ? 'Checking...' : '↻ Check Status Again'}
                </button>
                <button type="button" className="partner-btn-ghost" onClick={handleSignOut}>
                  Sign out
                </button>
              </div>
            </div>
          )}

          {/* APPROVED PARTNER SCREEN */}
          {isApproved && (() => {
            const availableJobs = jobs.filter((j) => j.status === 'available')
            const activeDeliveries = jobs.filter(
              (j) => j.status === 'assigned' || j.status === 'picked_up'
            )

            return (
              <div className="partner-approved-view">
                <section className="partner-welcome-banner">
                  <div>
                    <div className="partner-status-badge badge-approved">
                      <span className="dot dot-teal" /> Active Approved Partner
                    </div>
                    <h1>Welcome, {partner.full_name}!</h1>
                    <p>Ready to fulfill campus deliveries to hostels and academic blocks.</p>
                  </div>
                  <div className="partner-metric-pills">
                    <div className="metric-pill">
                      <small>Completed</small>
                      <strong>{partner.total_deliveries_completed || 0}</strong>
                    </div>
                    <div className="metric-pill">
                      <small>Active Deliveries</small>
                      <strong className="text-teal">{activeDeliveries.length}</strong>
                    </div>
                    <div className="metric-pill">
                      <small>Partner Rating</small>
                      <strong>★ {partner.rating || '5.00'}</strong>
                    </div>
                  </div>
                </section>

                {actionSuccess && (
                  <div className="partner-alert partner-alert-success" role="status">
                    <span className="alert-icon">✓</span>
                    <div className="alert-text">{actionSuccess}</div>
                    <button
                      type="button"
                      className="partner-alert-close"
                      onClick={() => setActionSuccess('')}
                      aria-label="Dismiss success message"
                    >
                      ✕
                    </button>
                  </div>
                )}

                {actionError && (
                  <div className="partner-alert partner-alert-error" role="alert">
                    <span className="alert-icon">!</span>
                    <div className="alert-text">{actionError}</div>
                    <button
                      type="button"
                      className="partner-alert-close"
                      onClick={() => setActionError('')}
                      aria-label="Dismiss error message"
                    >
                      ✕
                    </button>
                  </div>
                )}

                {jobsNotice && <div className="partner-alert partner-alert-info">{jobsNotice}</div>}

                {/* SECTION 1: MY ACTIVE DELIVERIES */}
                <section className="partner-section partner-active-board">
                  <div className="job-board-header">
                    <div>
                      <div className="partner-heading-row">
                        <h2>My Active Deliveries</h2>
                        <span className="partner-count-pill">{activeDeliveries.length} active</span>
                      </div>
                      <p>Orders assigned to you. Pick them up from the print shop and deliver to the student.</p>
                    </div>
                    <button
                      type="button"
                      className="partner-btn-secondary"
                      onClick={() => loadJobs(token)}
                      disabled={jobsLoading}
                    >
                      {jobsLoading ? '↻ Loading...' : '↻ Refresh'}
                    </button>
                  </div>

                  {activeDeliveries.length === 0 ? (
                    <div className="partner-empty-active">
                      <span className="empty-icon">📦</span>
                      <h3>No active deliveries</h3>
                      <p>
                        You have no claimed deliveries right now. Check the Available Delivery Jobs below to accept a job and start earning fees.
                      </p>
                    </div>
                  ) : (
                    <div className="partner-jobs-list">
                      {activeDeliveries.map((job) => (
                        <div key={job.id} className="partner-job-card partner-active-card">
                          <div className="job-card-top">
                            <div>
                              <strong>Order #{job.order_id}</strong>
                              <small className="partner-job-ref">Job #{job.id}</small>
                            </div>
                            <div className="job-top-right">
                              <span className={`job-status-pill status-${job.status}`}>
                                <span className="dot" />
                                {job.status === 'assigned' ? 'Ready for Pickup' : 'En Route to Student'}
                              </span>
                              <span className="job-fee-badge">₹{Number(job.delivery_fee || 5).toFixed(2)}</span>
                            </div>
                          </div>

                          <div className="job-card-body">
                            <p><strong>Deliver to:</strong> {job.delivery_location_name || 'Campus location'}</p>
                            {job.delivery_room_details && (
                              <small className="job-notes">Room/Notes: {job.delivery_room_details}</small>
                            )}
                            {job.delivery_time_window && (
                              <small className="job-window">Time window: {job.delivery_time_window}</small>
                            )}
                            <div className="job-timestamps">
                              {job.accepted_at && (
                                <span>Accepted: {new Date(job.accepted_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</span>
                              )}
                              {job.picked_up_at && (
                                <span>Picked up: {new Date(job.picked_up_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</span>
                              )}
                            </div>
                          </div>

                          <div className="job-card-bottom">
                            <span>Student: {job.student_name}</span>
                            {job.status === 'assigned' ? (
                              <button
                                type="button"
                                className="partner-btn-primary partner-btn-pickup"
                                disabled={actionLoadingId === job.id}
                                onClick={() => handlePickupJob(job.id)}
                              >
                                {actionLoadingId === job.id ? 'Updating...' : '📦 Mark Picked Up ↗'}
                              </button>
                            ) : (
                              <button
                                type="button"
                                className="partner-btn-success partner-btn-deliver"
                                disabled={actionLoadingId === job.id}
                                onClick={() => handleDeliverJob(job.id)}
                              >
                                {actionLoadingId === job.id ? 'Confirming...' : '✓ Confirm Delivery'}
                              </button>
                            )}
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </section>

                {/* SECTION 2: AVAILABLE DELIVERY JOBS */}
                <section className="partner-section partner-job-board">
                  <div className="job-board-header">
                    <div>
                      <div className="partner-heading-row">
                        <h2>Available Delivery Jobs</h2>
                        <span className="partner-count-pill pill-available">{availableJobs.length} available</span>
                      </div>
                      <p>Print jobs ready for campus delivery. Claim orders to deliver and earn fees.</p>
                    </div>
                    <button
                      type="button"
                      className="partner-btn-secondary"
                      onClick={() => loadJobs(token)}
                      disabled={jobsLoading}
                    >
                      {jobsLoading ? '↻ Loading...' : '↻ Refresh Board'}
                    </button>
                  </div>

                  {availableJobs.length === 0 ? (
                    <div className="partner-empty-jobs">
                      <span className="empty-icon">🚴</span>
                      <h3>No available delivery jobs right now</h3>
                      <p>
                        Orders marked for campus delivery will appear here as soon as students place them.
                      </p>
                      <small>Customer delivery fee: ₹5.00 per order</small>
                    </div>
                  ) : (
                    <div className="partner-jobs-list">
                      {availableJobs.map((job) => (
                        <div key={job.id} className="partner-job-card">
                          <div className="job-card-top">
                            <strong>Order #{job.order_id}</strong>
                            <span className="job-fee-badge">₹{Number(job.delivery_fee || 5).toFixed(2)}</span>
                          </div>

                          <div className="job-card-body">
                            <p><strong>Deliver to:</strong> {job.delivery_location_name || 'Campus location'}</p>
                            {job.delivery_room_details && (
                              <small className="job-notes">Room/Notes: {job.delivery_room_details}</small>
                            )}
                            {job.delivery_time_window && (
                              <small className="job-window">Time window: {job.delivery_time_window}</small>
                            )}
                          </div>

                          <div className="job-card-bottom">
                            <span>Student: {job.student_name}</span>
                            <button
                              type="button"
                              className="partner-btn-primary partner-btn-sm"
                              disabled={actionLoadingId === job.id}
                              onClick={() => handleAcceptJob(job.id)}
                            >
                              {actionLoadingId === job.id ? 'Claiming...' : 'Accept Job ↗'}
                            </button>
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </section>
              </div>
            )
          })()}
        </main>
      </div>
    )
  }

  // --- AUTHENTICATION SCREEN (LOGIN / SIGNUP) ---
  const currentDerivedEmail = deriveCollegeEmail(studentId)

  return (
    <div className="partner-auth-page">
      <aside className="partner-showcase">
        <PartnerBrand />

        <div className="partner-showcase-copy">
          <span className="showcase-tag">CAMPUS DELIVERY PARTNER</span>
          <h1>
            Deliver notes.
            <br />
            Earn on campus.
            <br />
            <span>Set your pace.</span>
          </h1>
          <p>
            Join the XeroxQueue student delivery network. Earn delivery fees by bringing printed notes directly to hostel blocks and academic buildings between classes.
          </p>

          <div className="partner-points">
            <div>
              <span>✓</span>
              <div>
                <strong>Flexible campus hours</strong>
                <small>Deliver only when you are free</small>
              </div>
            </div>
            <div>
              <span>✓</span>
              <div>
                <strong>Fixed ₹5 delivery fee</strong>
                <small>Earn per completed delivery</small>
              </div>
            </div>
            <div>
              <span>✓</span>
              <div>
                <strong>Student-to-student trusted network</strong>
                <small>Admin verified with your college roll number</small>
              </div>
            </div>
          </div>
        </div>

        <div className="partner-showcase-footer">
          <span>XeroxQueue · Campus Delivery Program</span>
        </div>
      </aside>

      <section className="partner-auth-main">
        <div className="partner-auth-card">
          <div className="partner-card-header">
            <span className="partner-subhead">DELIVERY PARTNER PORTAL</span>
            <h2>{authMode === 'signup' ? 'Apply to become a partner' : 'Partner sign in'}</h2>
            <p>
              {authMode === 'signup'
                ? 'Register with your student details. Applications require admin review.'
                : 'Sign in to check your application status or access delivery jobs.'}
            </p>
          </div>

          <div className="partner-auth-tabs">
            <button
              type="button"
              className={authMode === 'login' ? 'active' : ''}
              onClick={() => { setAuthMode('login'); setError(''); setSuccess(''); setMigrationNotice('') }}
            >
              Sign in
            </button>
            <button
              type="button"
              className={authMode === 'signup' ? 'active' : ''}
              onClick={() => { setAuthMode('signup'); setError(''); setSuccess(''); setMigrationNotice('') }}
            >
              Apply to deliver
            </button>
          </div>

          {migrationNotice && (
            <div className="partner-alert partner-alert-warning" role="alert">
              <strong>Database Migration Required:</strong>
              <p>{migrationNotice}</p>
              <small>Run <code>001_campus_delivery_system.sql</code> on xeroxqueue_db to activate delivery tables.</small>
            </div>
          )}

          {error && <div className="partner-alert partner-alert-error" role="alert">{error}</div>}
          {success && <div className="partner-alert partner-alert-success" role="status">{success}</div>}

          {authMode === 'signup' ? (
            <form onSubmit={handleSignup} className="partner-form">
              <div className="field">
                <label htmlFor="partner-name">Full name</label>
                <input
                  id="partner-name"
                  type="text"
                  placeholder="e.g. Alex Johnson"
                  value={fullName}
                  onChange={(e) => setFullName(e.target.value)}
                  required
                  maxLength={100}
                />
              </div>

              <div className="field">
                <label htmlFor="partner-id">Student ID (College Roll Number)</label>
                <input
                  id="partner-id"
                  type="text"
                  placeholder="e.g. 241fa04072"
                  value={studentId}
                  onChange={(e) => setStudentId(e.target.value)}
                  required
                  maxLength={50}
                  autoComplete="username"
                />
              </div>

              <div className="partner-derived-email-card">
                <div className="email-card-icon">✉</div>
                <div>
                  <small>Auto-generated college email</small>
                  <strong>{currentDerivedEmail}</strong>
                  <span>Derived automatically from your student ID.</span>
                </div>
              </div>

              <div className="field">
                <label htmlFor="partner-phone">Phone number</label>
                <input
                  id="partner-phone"
                  type="tel"
                  placeholder="e.g. 9876543210"
                  value={phoneNumber}
                  onChange={(e) => setPhoneNumber(e.target.value)}
                  required
                  maxLength={20}
                  autoComplete="tel"
                />
              </div>

              <div className="field">
                <label htmlFor="partner-password">Password</label>
                <div className="partner-password-wrap">
                  <input
                    id="partner-password"
                    type={showPassword ? 'text' : 'password'}
                    placeholder="Create a password (min 8 characters)"
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    required
                    minLength={8}
                    maxLength={128}
                    autoComplete="new-password"
                  />
                  <button
                    type="button"
                    className="partner-password-toggle"
                    onClick={() => setShowPassword(!showPassword)}
                  >
                    {showPassword ? 'Hide' : 'Show'}
                  </button>
                </div>
                <small className="partner-hint">Stored securely using Argon2 password hashing.</small>
              </div>

              <button type="submit" className="partner-btn-primary partner-btn-block" disabled={loading}>
                {loading ? 'Submitting Application...' : 'Submit Partner Application ↗'}
              </button>
            </form>
          ) : (
            <form onSubmit={handleLogin} className="partner-form">
              <div className="field">
                <label htmlFor="partner-login-id">Student ID, Phone, or College Email</label>
                <input
                  id="partner-login-id"
                  type="text"
                  placeholder="e.g. 241fa04072 or vu.241fa04072@gmail.com"
                  value={studentId}
                  onChange={(e) => setStudentId(e.target.value)}
                  required
                  autoComplete="username"
                />
              </div>

              <div className="field">
                <label htmlFor="partner-login-password">Password</label>
                <div className="partner-password-wrap">
                  <input
                    id="partner-login-password"
                    type={showPassword ? 'text' : 'password'}
                    placeholder="Enter your partner password"
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    required
                    autoComplete="current-password"
                  />
                  <button
                    type="button"
                    className="partner-password-toggle"
                    onClick={() => setShowPassword(!showPassword)}
                  >
                    {showPassword ? 'Hide' : 'Show'}
                  </button>
                </div>
              </div>

              <button type="submit" className="partner-btn-primary partner-btn-block" disabled={loading}>
                {loading ? 'Signing in...' : 'Sign In as Delivery Partner ↗'}
              </button>
            </form>
          )}

          <div className="partner-card-footer">
            <button type="button" className="partner-btn-back" onClick={onBack}>
              ← Back to Student Portal
            </button>
          </div>
        </div>
      </section>
    </div>
  )
}
