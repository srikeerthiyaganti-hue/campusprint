import { useCallback, useEffect, useState } from 'react'
import './App.css'
import AdminDashboard from './AdminDashboard'
import PartnerPortal from './PartnerPortal'
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

const CURRENT_YEAR = new Date().getFullYear()

function Brand({ light = false }) {
  return (
    <div className={`brand ${light ? 'brand-light' : ''}`}>
      <div className="brand-symbol">
        <span>Q</span>
        <span className="brand-symbol-dot" />
      </div>
      <div>
        <div className="brand-name">XeroxQueue</div>
        <div className="brand-tagline">Print smarter. Study better.</div>
      </div>
    </div>
  )
}

function AuthArtwork({ signup }) {
  return (
    <aside className="auth-showcase">
      <div className="showcase-glow showcase-glow-one" />
      <div className="showcase-glow showcase-glow-two" />

      <Brand light />

      <div className="showcase-copy">
        <div className="showcase-eyebrow">
          <span className="live-dot" />
          YOUR CAMPUS, SIMPLIFIED
        </div>

        <h1>
          Your notes.
          <br />
          Your time.
          <br />
          <span>Your success.</span>
        </h1>

        <p>
          A smarter way to get your college documents printed.
          Upload your PDFs, choose your preferences, and keep
          track of every order in one place.
        </p>

        <div className="showcase-features">
          <div className="showcase-feature">
            <span className="feature-icon">↥</span>
            <div>
              <strong>Easy PDF uploads</strong>
              <span>Send your documents in seconds</span>
            </div>
            <span className="feature-check">✓</span>
          </div>

          <div className="showcase-feature">
            <span className="feature-icon feature-icon-teal">◷</span>
            <div>
              <strong>Track your orders</strong>
              <span>Stay updated at every step</span>
            </div>
            <span className="feature-check">✓</span>
          </div>

          <div className="showcase-feature">
            <span className="feature-icon feature-icon-purple">▤</span>
            <div>
              <strong>Your personal history</strong>
              <span>Keep your print orders organized</span>
            </div>
            <span className="feature-check">✓</span>
          </div>
        </div>
      </div>

      <div className="showcase-bottom">
        <div className="avatar-stack" aria-hidden="true">
          <span>K</span>
          <span>S</span>
          <span>A</span>
          <span>+</span>
        </div>
        <p>
          <strong>Made for student life</strong>
          <span>Less waiting. More learning.</span>
        </p>
      </div>

      <div className="showcase-decoration" aria-hidden="true">
        <div className="decoration-sheet sheet-back" />
        <div className="decoration-sheet sheet-front">
          <div className="sheet-mark">Q.</div>
          <div className="sheet-line sheet-line-long" />
          <div className="sheet-line" />
          <div className="sheet-line sheet-line-short" />
          <div className="sheet-mini-block" />
          <div className="sheet-line sheet-line-long" />
          <div className="sheet-line" />
          <div className="sheet-stamp">PDF</div>
        </div>
        <div className="floating-chip">
          <span>✓</span> Ready when you are
        </div>
      </div>

      <div className="showcase-footer">
        <span>© {CURRENT_YEAR} XeroxQueue</span>
        <span>{signup ? 'A better way to get started' : 'Welcome to a smarter campus'}</span>
      </div>
    </aside>
  )
}

function AuthPage({
  authView,
  setAuthView,
  onLogin,
  onSignup,
  onAdminClick,
  onPartnerClick,
  loading,
  message,
  setMessage,
}) {
  const signup = authView === 'signup'

  const [fullName, setFullName] = useState('')
  const [studentId, setStudentId] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [showPassword, setShowPassword] = useState(false)

  function switchView(view) {
    setAuthView(view)
    setMessage('')
    setPassword('')
  }

  async function handleSubmit(event) {
    event.preventDefault()

    if (signup) {
      await onSignup({
        full_name: fullName.trim(),
        student_id: studentId.trim(),
        email: email.trim(),
        password,
      })
    } else {
      await onLogin({
        identifier: studentId.trim(),
        password,
      })
    }
  }

  return (
    <main className="auth-page">
      <AuthArtwork signup={signup} />

      <section className="auth-main">
        <div className="mobile-brand">
          <Brand />
        </div>

        <div className="auth-form-wrap">
          <div className="auth-topline">
            <span className="auth-step">
              <span className="auth-step-dot" />
              STUDENT PORTAL
            </span>

            <span className="secure-label">
              <span>⌑</span> Secure access
            </span>
          </div>

          <div className="auth-heading">
            <div className="auth-icon">
              {signup ? '✳' : '↗'}
            </div>

            <h2>
              {signup ? 'Create your account' : 'Welcome back'}
            </h2>

            <p>
              {signup
                ? 'Create your student account and make printing simpler.'
                : 'Your campus printing, all in one place.'}
            </p>
          </div>

          <div className="auth-tabs">
            <button
              type="button"
              className={!signup ? 'auth-tab active' : 'auth-tab'}
              onClick={() => switchView('login')}
            >
              Sign in
            </button>

            <button
              type="button"
              className={signup ? 'auth-tab active' : 'auth-tab'}
              onClick={() => switchView('signup')}
            >
              Create account
            </button>
          </div>

          {message && (
            <div
              className={`form-message ${message.type === 'error' ? 'form-message-error' : 'form-message-success'}`}
              role="status"
            >
              <span>{message.type === 'error' ? '!' : '✓'}</span>
              <p>{message.text}</p>
            </div>
          )}

          <form className="auth-form" onSubmit={handleSubmit}>
            {signup && (
              <div className="field">
                <label htmlFor="full-name">Full name</label>
                <div className="input-shell">
                  <span className="input-icon">♙</span>
                  <input
                    id="full-name"
                    type="text"
                    placeholder="Enter your full name"
                    value={fullName}
                    onChange={(event) => setFullName(event.target.value)}
                    autoComplete="name"
                    required
                    maxLength={100}
                  />
                </div>
              </div>
            )}

            <div className="field">
              <label htmlFor="student-id">
                {signup ? 'Student ID' : 'Student ID or email'}
              </label>
              <div className="input-shell">
                <span className="input-icon">▤</span>
                <input
                  id="student-id"
                  type="text"
                  placeholder={
                    signup
                      ? 'Enter your student ID'
                      : 'Enter your student ID or email'
                  }
                  value={studentId}
                  onChange={(event) => setStudentId(event.target.value)}
                  autoComplete="username"
                  required
                  maxLength={255}
                />
              </div>
            </div>

            {signup && (
              <div className="field">
                <label htmlFor="student-email">College email</label>
                <div className="input-shell">
                  <span className="input-icon">✉</span>
                  <input
                    id="student-email"
                    type="email"
                    placeholder="you@example.com"
                    value={email}
                    onChange={(event) => setEmail(event.target.value)}
                    autoComplete="email"
                    required
                    maxLength={255}
                  />
                </div>
              </div>
            )}

            <div className="field">
              <div className="field-label-row">
                <label htmlFor="student-password">Password</label>
                {!signup && (
                  <span className="field-hint">Your account password</span>
                )}
              </div>

              <div className="input-shell">
                <span className="input-icon">⌑</span>
                <input
                  id="student-password"
                  type={showPassword ? 'text' : 'password'}
                  placeholder={
                    signup ? 'Create a password' : 'Enter your password'
                  }
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  autoComplete={signup ? 'new-password' : 'current-password'}
                  required
                  maxLength={128}
                />

                <button
                  type="button"
                  className="password-toggle"
                  onClick={() => setShowPassword((current) => !current)}
                  aria-label={showPassword ? 'Hide password' : 'Show password'}
                >
                  {showPassword ? 'Hide' : 'Show'}
                </button>
              </div>

              {signup && (
                <p className="input-help">
                  Choose a password that is difficult to guess.
                </p>
              )}
            </div>

            <button
              className="auth-submit"
              type="submit"
              disabled={loading}
            >
              {loading ? (
                <>
                  <span className="button-spinner" />
                  Please wait...
                </>
              ) : (
                <>
                  {signup ? 'Create my account' : 'Sign in to XeroxQueue'}
                  <span className="button-arrow">↗</span>
                </>
              )}
            </button>
          </form>

          <div className="auth-switch">
            <span>
              {signup
                ? 'Already have an account?'
                : 'New to XeroxQueue?'}
            </span>
            <button
              type="button"
              onClick={() => switchView(signup ? 'login' : 'signup')}
            >
              {signup ? 'Sign in' : 'Create an account'}
            </button>
          </div>

          <div className="auth-switch">
            <span>Campus delivery partner?</span>
            <button
              type="button"
              onClick={onPartnerClick}
            >
              Sign in or join
            </button>
          </div>

          {!signup && (
            <div className="auth-switch">
              <span>Print shop administrator?</span>
              <button
                type="button"
                onClick={onAdminClick}
              >
                Admin sign in
              </button>
            </div>
          )}


          <div className="auth-assurance">
            <span className="assurance-icon">✓</span>
            <p>
              Your personal orders stay linked to your student account.
            </p>
          </div>
        </div>

        <div className="auth-main-footer">
          <span>© {CURRENT_YEAR} XeroxQueue</span>
          <span>Built for campus life</span>
        </div>
      </section>
    </main>
  )
}

function OrderStatus({ status }) {
  const normalized = String(status || 'pending').toLowerCase()

  const className = normalized
    .replace(/[^a-z0-9-]/g, '-')
    .replace(/-+/g, '-')

  return (
    <span className={`status-pill status-${className}`}>
      <span className="status-dot" />
      {normalized.replace(/[-_]/g, ' ')}
    </span>
  )
}

function formatDeliveryTime(timestamp) {
  if (!timestamp) return null
  try {
    const isoStr = typeof timestamp === 'string' ? timestamp.replace(' ', 'T') : timestamp
    const d = new Date(isoStr)
    if (isNaN(d.getTime())) return null
    return d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
  } catch {
    return null
  }
}

function formatDeliveryDateTime(timestamp) {
  if (!timestamp) return null
  try {
    const isoStr = typeof timestamp === 'string' ? timestamp.replace(' ', 'T') : timestamp
    const d = new Date(isoStr)
    if (isNaN(d.getTime())) return null
    return `${d.toLocaleDateString([], { month: 'short', day: 'numeric' })}, ${d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}`
  } catch {
    return null
  }
}

const DELIVERY_STEPS = [
  { step: 1, key: 'available', label: 'Awaiting partner' },
  { step: 2, key: 'assigned', label: 'Partner assigned' },
  { step: 3, key: 'picked_up', label: 'Order picked up' },
  { step: 4, key: 'delivered', label: 'Delivered' },
]

function getDeliveryStatusInfo(jobStatus) {
  switch (jobStatus) {
    case 'assigned':
      return {
        key: 'assigned',
        label: 'Partner assigned',
        step: 2,
        badgeClass: 'delivery-badge-assigned',
        description: 'A delivery partner has accepted your delivery and will collect your prints when ready.',
      }
    case 'picked_up':
      return {
        key: 'picked_up',
        label: 'Order picked up',
        step: 3,
        badgeClass: 'delivery-badge-picked_up',
        description: 'Your order has been picked up from the xerox shop and is on the way to you.',
      }
    case 'delivered':
      return {
        key: 'delivered',
        label: 'Delivered',
        step: 4,
        badgeClass: 'delivery-badge-delivered',
        description: 'Your print order has been delivered successfully to your campus location.',
      }
    case 'cancelled':
      return {
        key: 'cancelled',
        label: 'Delivery cancelled',
        step: 0,
        badgeClass: 'delivery-badge-cancelled',
        description: 'This delivery job was cancelled.',
      }
    case 'available':
    default:
      return {
        key: 'available',
        label: 'Awaiting delivery partner',
        step: 1,
        badgeClass: 'delivery-badge-available',
        description: 'Waiting for an approved student delivery partner to accept your delivery request.',
      }
  }
}

function getStepTimestamp(order, stepNumber) {
  if (stepNumber === 1) return formatDeliveryTime(order.created_at)
  if (stepNumber === 2) return formatDeliveryTime(order.delivery_accepted_at)
  if (stepNumber === 3) return formatDeliveryTime(order.delivery_picked_up_at)
  if (stepNumber === 4) return formatDeliveryTime(order.delivery_delivered_at)
  return null
}

function getLatestDeliveryTimestamp(order) {
  if (order.delivery_delivered_at) return formatDeliveryDateTime(order.delivery_delivered_at)
  if (order.delivery_picked_up_at) return formatDeliveryDateTime(order.delivery_picked_up_at)
  if (order.delivery_accepted_at) return formatDeliveryDateTime(order.delivery_accepted_at)
  if (order.created_at) return formatDeliveryDateTime(order.created_at)
  return null
}

function StudentDeliveryTracker({ order }) {
  const statusInfo = getDeliveryStatusInfo(order.delivery_job_status)
  const isCancelled = order.delivery_job_status === 'cancelled'
  const latestTimestamp = getLatestDeliveryTimestamp(order)

  return (
    <div className="order-delivery-tracking">
      <div className="delivery-tracking-header">
        <div className="delivery-tracking-title">
          <span className="delivery-tracking-icon" aria-hidden="true">🚴</span>
          <div>
            <span className="delivery-tracking-eyebrow">Campus Delivery</span>
            <strong className="delivery-tracking-status-text">{statusInfo.label}</strong>
          </div>
        </div>
        <span className={`delivery-status-badge ${statusInfo.badgeClass}`}>
          <span className="delivery-status-pulse" />
          {statusInfo.label}
        </span>
      </div>

      <p className="delivery-tracking-desc">{statusInfo.description}</p>

      {!isCancelled && (
        <div className="delivery-stepper" aria-label="Delivery progress tracker">
          {DELIVERY_STEPS.map((stepItem, idx) => {
            const isCompleted = statusInfo.step > stepItem.step
            const isCurrent = statusInfo.step === stepItem.step
            const stepTime = getStepTimestamp(order, stepItem.step)

            return (
              <div
                key={stepItem.step}
                className={`stepper-node ${isCompleted ? 'is-completed' : ''} ${isCurrent ? 'is-current' : ''}`}
              >
                <div className="stepper-indicator-row">
                  <div className="stepper-dot">
                    {isCompleted ? '✓' : stepItem.step}
                  </div>
                  {idx < DELIVERY_STEPS.length - 1 && (
                    <div
                      className={`stepper-connector ${statusInfo.step > stepItem.step ? 'connector-filled' : ''}`}
                    />
                  )}
                </div>
                <div className="stepper-label-block">
                  <span className="stepper-label">{stepItem.label}</span>
                  {stepTime && <span className="stepper-time">{stepTime}</span>}
                </div>
              </div>
            )
          })}
        </div>
      )}

      <div className="delivery-meta-grid">
        <div className="delivery-meta-item">
          <span className="delivery-meta-label">Destination</span>
          <strong className="delivery-meta-value">
            {order.delivery_location_name || order.delivery_location || 'Campus Delivery'}
            {order.delivery_room_details ? ` (${order.delivery_room_details})` : ''}
          </strong>
        </div>

        {order.delivery_partner_name && (
          <div className="delivery-meta-item">
            <span className="delivery-meta-label">Delivery Partner</span>
            <strong className="delivery-meta-value">{order.delivery_partner_name}</strong>
          </div>
        )}

        <div className="delivery-meta-item">
          <span className="delivery-meta-label">Delivery Fee</span>
          <strong className="delivery-meta-value">
            ₹{order.delivery_fee != null ? Number(order.delivery_fee).toFixed(0) : '5'}
          </strong>
        </div>

        {latestTimestamp && (
          <div className="delivery-meta-item">
            <span className="delivery-meta-label">Latest Update</span>
            <strong className="delivery-meta-value">{latestTimestamp}</strong>
          </div>
        )}
      </div>
    </div>
  )
}

function OrderCard({ order }) {
  const date = order.created_at
    ? new Date(order.created_at).toLocaleString()
    : 'Date unavailable'

  const isDelivery = order.fulfillment_type === 'delivery'

  return (
    <article className={`order-item ${isDelivery ? 'order-item-delivery' : ''}`}>
      <div className="order-file-icon">PDF</div>

      <div className="order-item-main">
        <div className="order-title-row">
          <h3>{order.file_name || 'Print document'}</h3>
          <OrderStatus status={order.status} />
        </div>

        <p className="order-date">{date}</p>

        <div className="order-detail-chips">
          <span>{order.copies} {Number(order.copies) === 1 ? 'copy' : 'copies'}</span>
          <span>
            {order.print_type === 'black_white'
              ? 'Black & White'
              : order.print_type === 'colour'
                ? 'Colour'
                : order.print_type || 'Black & White'}
          </span>
          <span>
            {order.print_sides === 'single'
              ? 'Single-sided'
              : order.print_sides === 'double'
                ? 'Double-sided'
                : order.print_sides || 'Single-sided'}
          </span>
          {order.fulfillment_type && (
            <span className={isDelivery ? 'chip-delivery' : 'chip-pickup'}>
              {isDelivery ? 'Campus delivery (₹5)' : 'Self pickup (₹0)'}
            </span>
          )}
        </div>

        {isDelivery && <StudentDeliveryTracker order={order} />}
      </div>

      <div className="order-number">
        <span>ORDER</span>
        <strong>#{order.id}</strong>
      </div>
    </article>
  )
}

function StudentDashboard({
  student,
  onLogout,
  accessToken,
  onOrderPlaced,
}) {
  const [backendStatus, setBackendStatus] = useState('Checking connection')
  const [orders, setOrders] = useState([])
  const [ordersStatus, setOrdersStatus] = useState('Loading your orders...')

const [file, setFile] = useState(null)
const [pageCount, setPageCount] = useState(null)
const [storedFileName, setStoredFileName] = useState(null)
const [isUploading, setIsUploading] = useState(false)

  const [copies, setCopies] = useState('1')
  const [printType, setPrintType] = useState('Black & White')
  const [printSides, setPrintSides] = useState('Single-sided')
  const [deliveryMethod, setDeliveryMethod] = useState('pickup')
  const [deliveryLocations, setDeliveryLocations] = useState([
    { id: null, location_code: 'HOSTEL_A', name: 'Boys Hostel Block A', description: 'Ground floor reception / security desk' },
    { id: null, location_code: 'HOSTEL_B', name: 'Girls Hostel Block B', description: 'Main gate security checkpoint' },
    { id: null, location_code: 'LIB_FOYER', name: 'Central Library Foyer', description: 'Library entrance turnstile lobby' },
    { id: null, location_code: 'ACAD_BLOCK_1', name: 'Academic Block 1', description: 'Main entrance lobby, near stairs' },
    { id: null, location_code: 'MGMT_BLOCK', name: 'Management Block', description: 'Department reception counter' },
    { id: null, location_code: 'CANTEEN', name: 'Campus Food Court / Canteen', description: 'Outdoor pickup table' },
  ])
  const [selectedLocationKey, setSelectedLocationKey] = useState('name:Boys Hostel Block A')
  const [deliveryRoomDetails, setDeliveryRoomDetails] = useState('')
  const [deliveryTimeWindow, setDeliveryTimeWindow] = useState('Standard (ASAP)')
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [orderMessage, setOrderMessage] = useState(null)

  const loadOrders = useCallback(async () => {
    try {
      const response = await fetch(`${API}/api/orders`, {
        headers: {
          Authorization: `Bearer ${accessToken}`,
        },
      })

      if (response.status === 401) {
        setOrdersStatus('Your session has expired. Please sign in again.')
        onLogout?.('Your session has expired. Please sign in again.')
        return
      }

      if (!response.ok) {
        throw new Error('Unable to load your orders.')
      }

      const data = await response.json()
      setOrders(Array.isArray(data.orders) ? data.orders : [])
      setOrdersStatus('')
    } catch {
      setOrdersStatus('Could not load orders. Check that the backend is running.')
    }
  }, [accessToken, onLogout])

  useEffect(() => {
    let cancelled = false

    async function checkBackend() {
      try {
        const response = await fetch(`${API}/api/health`)
        if (!cancelled) {
          setBackendStatus(response.ok ? 'Connected' : 'Connection issue')
        }
      } catch {
        if (!cancelled) setBackendStatus('Offline')
      }
    }

    async function loadLocations() {
      try {
        const response = await fetch(`${API}/api/locations`)
        if (!response.ok) return
        const data = await response.json()
        if (!cancelled && Array.isArray(data.locations) && data.locations.length > 0) {
          setDeliveryLocations(data.locations)
          const first = data.locations[0]
          const key = first.id !== null && first.id !== undefined ? `id:${first.id}` : `name:${first.name}`
          setSelectedLocationKey(key)
        }
      } catch {
        // Retain fallback list
      }
    }

    async function fetchInitialData() {
      checkBackend()
      loadLocations()
      if (!cancelled) {
        await loadOrders()
      }
    }

    fetchInitialData()

    const interval = window.setInterval(() => {
      if (!cancelled) loadOrders()
    }, 10000)

    return () => {
      cancelled = true
      window.clearInterval(interval)
    }
  }, [loadOrders])




async function handleFileChange(event) {
  const selectedFile = event.target.files?.[0] || null

  setFile(selectedFile)
  setPageCount(null)
  setStoredFileName(null)
  setOrderMessage(null)

  if (!selectedFile) return

  if (
    selectedFile.type !== 'application/pdf' &&
    !selectedFile.name.toLowerCase().endsWith('.pdf')
  ) {
    setOrderMessage({
      type: 'error',
      text: 'Please upload a PDF document.',
    })
    return
  }

  setIsUploading(true)

  try {
    const uploadData = new FormData()
    uploadData.append('file', selectedFile)

    const response = await fetch(`${API}/api/upload`, {
  method: 'POST',
  headers: {
    Authorization: `Bearer ${accessToken}`,
  },
  body: uploadData,
})

    const data = await response.json().catch(() => ({}))

    if (!response.ok) {
      throw new Error(
        typeof data.detail === 'string'
          ? data.detail
          : 'PDF upload failed.'
      )
    }

    if (
      !data.stored_file_name ||
      !Number.isInteger(data.page_count) ||
      data.page_count < 1
    ) {
      throw new Error(
        'Could not read the PDF page count. Please try again.'
      )
    }

    setStoredFileName(data.stored_file_name)
    setPageCount(data.page_count)
  } catch (error) {
    setOrderMessage({
      type: 'error',
      text: error.message || 'Could not upload the PDF.',
    })
  } finally {
    setIsUploading(false)
  }
}

async function handlePlaceOrder(event) {
  event.preventDefault()
  setOrderMessage(null)

  if (!file) {
    setOrderMessage({
      type: 'error',
      text: 'Please select a PDF file before placing your order.',
    })
    return
  }

  if (isUploading) {
    setOrderMessage({
      type: 'error',
      text: 'Please wait until your PDF upload finishes.',
    })
    return
  }

  if (!storedFileName || pageCount === null) {
    setOrderMessage({
      type: 'error',
      text: 'Please select a valid PDF and wait for its page count.',
    })
    return
  }

  const copyCount = Number(copies)

  if (!Number.isInteger(copyCount) || copyCount < 1 || copyCount > 100) {
    setOrderMessage({
      type: 'error',
      text: 'Choose a copy count between 1 and 100.',
    })
    return
  }

  if (deliveryMethod === 'delivery') {
    const trimmedRoom = (deliveryRoomDetails || '').trim()
    if (!trimmedRoom) {
      setOrderMessage({
        type: 'error',
        text: 'Please specify your room number or delivery details (e.g. Room 204, 2nd Floor).',
      })
      return
    }
    if (trimmedRoom.length > 255) {
      setOrderMessage({
        type: 'error',
        text: 'Delivery room details must be 255 characters or fewer.',
      })
      return
    }
  }

  setIsSubmitting(true)

  try {
    const backendPrintType =
      printType === 'Black & White' ? 'black_white' : 'colour'

    const backendPrintSides =
      printSides === 'Single-sided' ? 'single' : 'double'

    const selectedLocation = deliveryLocations.find((loc) => {
      const key = loc.id !== null && loc.id !== undefined ? `id:${loc.id}` : `name:${loc.name}`
      return key === selectedLocationKey
    }) || deliveryLocations[0] || null

    const orderResponse = await fetch(`${API}/api/orders`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${accessToken}`,
      },
      body: JSON.stringify({
        student_name: student?.full_name || student?.name || '',
        file_name: file.name,
        stored_file_name: storedFileName,
        copies: copyCount,
        print_type: backendPrintType,
        print_sides: backendPrintSides,
        fulfillment_type: deliveryMethod,
        delivery_location_id: deliveryMethod === 'delivery' ? (selectedLocation?.id ?? null) : null,
        delivery_location_name: deliveryMethod === 'delivery' ? (selectedLocation?.name ?? null) : null,
        delivery_room_details: deliveryMethod === 'delivery' ? (deliveryRoomDetails || '').trim() : null,
        delivery_time_window: deliveryMethod === 'delivery' ? (deliveryTimeWindow || null) : null,
      }),
    })

    const orderData = await orderResponse.json().catch(() => ({}))

    if (!orderResponse.ok) {
      const detail = orderData.detail
      const message = Array.isArray(detail)
        ? detail.map((item) => item.msg || JSON.stringify(item)).join('; ')
        : typeof detail === 'string'
          ? detail
          : 'Could not place the print order.'

      throw new Error(`Order submission failed: ${message}`)
    }

    setFile(null)
    setPageCount(null)
    setStoredFileName(null)
    setCopies('1')
    setPrintType('Black & White')
    setPrintSides('Single-sided')
    setDeliveryMethod('pickup')
    setDeliveryRoomDetails('')
    setDeliveryTimeWindow('Standard (ASAP)')

    const fileInput = document.getElementById('pdf-upload')
    if (fileInput) fileInput.value = ''

    setOrderMessage({
      type: 'success',
      text: `Your print order #${orderData.order_id} was placed successfully.`,
    })

    await loadOrders()
    onOrderPlaced?.()
  } catch (error) {
    setOrderMessage({
      type: 'error',
      text: error.message || 'Something went wrong. Please try again.',
    })
  } finally {
    setIsSubmitting(false)
  }
}



  const pendingCount = orders.filter(
    (order) => String(order.status).toLowerCase() === 'pending'
  ).length

  const readyCount = orders.filter(
    (order) => String(order.status).toLowerCase() === 'ready'
  ).length

const ratePerSheet = printType === 'Black & White' ? 2 : 10

const sheetsPerCopy =
  pageCount === null
    ? 0
    : Math.ceil(
        pageCount / (printSides === 'Double-sided' ? 2 : 1)
      )

const estimateCopyCount = Number(copies)

const validEstimateCopies =
  Number.isInteger(estimateCopyCount) &&
  estimateCopyCount >= 1 &&
  estimateCopyCount <= 100

const estimatedSheets = validEstimateCopies
  ? sheetsPerCopy * estimateCopyCount
  : 0

const estimatedPrintingCost = estimatedSheets * ratePerSheet
const deliveryFee = deliveryMethod === 'delivery' ? 5 : 0
const totalEstimatedPrice = estimatedPrintingCost + deliveryFee


  return (
    <div className="dashboard-page">
      <header className="dashboard-nav">
        <Brand />

        <div className="dashboard-nav-right">
          <span className={`connection-pill ${backendStatus === 'Connected' ? 'connection-online' : ''}`}>
            <span className="status-dot" />
            {backendStatus}
          </span>

          <div className="user-profile">
            <div className="user-avatar">
              {(student.full_name || 'S').trim().charAt(0).toUpperCase()}
            </div>
            <div className="user-profile-copy">
              <strong>{student.full_name}</strong>
              <span>{student.student_id}</span>
            </div>
          </div>

          <button className="logout-button" onClick={onLogout}>
            Sign out <span>↗</span>
          </button>
        </div>
      </header>

      <main className="dashboard-content">
        <section className="dashboard-welcome">
          <div>
            <div className="welcome-eyebrow">
              <span className="live-dot" />
              STUDENT WORKSPACE
            </div>

            <h1>
              Hello, {student.full_name?.split(' ')[0] || 'Student'}.
              <br />
              <span>Let's get printing.</span>
            </h1>

            <p>
              Upload your documents, choose your print settings,
              and keep track of your campus print orders.
            </p>
          </div>

          <div className="welcome-illustration" aria-hidden="true">
            <div className="illustration-circle" />
            <div className="illustration-paper">
              <div className="illustration-paper-top">Q.</div>
              <div className="illustration-line long" />
              <div className="illustration-line" />
              <div className="illustration-line short" />
              <div className="illustration-block" />
              <div className="illustration-line long" />
              <div className="illustration-line" />
            </div>
            <div className="illustration-check">✓</div>
          </div>
        </section>

        <section className="stats-row">
          <article className="stat-tile">
            <div className="stat-icon stat-icon-blue">▤</div>
            <div>
              <span>Total orders</span>
              <strong>{orders.length}</strong>
              <small>In your account</small>
            </div>
          </article>

          <article className="stat-tile">
            <div className="stat-icon stat-icon-amber">◷</div>
            <div>
              <span>In progress</span>
              <strong>{pendingCount}</strong>
              <small>Awaiting processing</small>
            </div>
          </article>

          <article className="stat-tile">
            <div className="stat-icon stat-icon-teal">✓</div>
            <div>
              <span>Ready orders</span>
              <strong>{readyCount}</strong>
              <small>Marked ready</small>
            </div>
          </article>
        </section>

        <section className="dashboard-grid">
          <article className="surface-card create-order-card">
            <div className="section-heading">
              <div className="section-heading-icon">↥</div>
              <div>
                <h2>Create a print order</h2>
                <p>Upload your PDF and select your preferences.</p>
              </div>
            </div>

            <form onSubmit={handlePlaceOrder}>
              <label className="upload-zone" htmlFor="pdf-upload">
                <span className="upload-symbol">↑</span>
                <strong>{file ? file.name : 'Choose your PDF document'}</strong>

                <span>
                  {!file
                    ? 'Select a PDF from your device'
                    : isUploading
                      ? 'Uploading PDF and counting pages...'
                      : pageCount !== null
                        ? `${pageCount} pages · ${(file.size / 1024 / 1024).toFixed(2)} MB · Click to change`
                        : `${(file.size / 1024 / 1024).toFixed(2)} MB · Click to change`}
                </span>

                <span className="upload-action">
                  {file ? 'Change document' : 'Browse files'}
                </span>

                <input
                  id="pdf-upload"
                  type="file"
                  accept=".pdf,application/pdf"
                  onChange={handleFileChange}
                />

              </label>

              <div className="field">
                <label htmlFor="copies">Number of copies</label>
                <input
                  id="copies"
                  type="number"
                  min="1"
                  max="100"
                  value={copies}
                  onChange={(event) => setCopies(event.target.value)}
                  required
                />
              </div>

              <div className="field">
                <label htmlFor="print-type">Print colour</label>
                <select
                  id="print-type"
                  value={printType}
                  onChange={(event) => setPrintType(event.target.value)}
                >
                  <option>Black & White</option>
                  <option>Color</option>
                </select>
              </div>

              <div className="field">
                <label htmlFor="print-sides">Page sides</label>
                <select
                  id="print-sides"
                  value={printSides}
                  onChange={(event) => setPrintSides(event.target.value)}
                >
                  <option>Single-sided</option>
                  <option>Double-sided</option>
                </select>
              </div>

              <div className="field">
                <label>Delivery method</label>
                <div className="delivery-toggle-group" role="radiogroup" aria-label="Delivery method">
                  <button
                    type="button"
                    className={`delivery-toggle-btn ${deliveryMethod === 'pickup' ? 'active' : ''}`}
                    onClick={() => setDeliveryMethod('pickup')}
                    aria-pressed={deliveryMethod === 'pickup'}
                  >
                    <div className="delivery-toggle-text">
                      <strong>Self pickup</strong>
                      <span>Collect from print shop</span>
                    </div>
                    <span className="delivery-fee-tag tag-free">₹0 fee</span>
                  </button>

                  <button
                    type="button"
                    className={`delivery-toggle-btn ${deliveryMethod === 'delivery' ? 'active' : ''}`}
                    onClick={() => setDeliveryMethod('delivery')}
                    aria-pressed={deliveryMethod === 'delivery'}
                  >
                    <div className="delivery-toggle-text">
                      <strong>Campus delivery</strong>
                      <span>Delivered to your building</span>
                    </div>
                    <span className="delivery-fee-tag tag-fee">₹5 fee</span>
                  </button>
                </div>
              </div>

              {deliveryMethod === 'delivery' && (
                <div
                  className="delivery-destination-card"
                  style={{
                    display: 'grid',
                    gap: '1rem',
                    padding: '1.25rem',
                    background: '#f8fafc',
                    borderRadius: '12px',
                    border: '1px solid #cbd5e1',
                    margin: '0.5rem 0 1rem 0',
                  }}
                >
                  <div className="field" style={{ margin: 0 }}>
                    <div className="field-label-row">
                      <label htmlFor="delivery-location">Campus delivery hub *</label>
                      <span className="field-hint">Select your building or area</span>
                    </div>
                    <select
                      id="delivery-location"
                      value={selectedLocationKey}
                      onChange={(event) => setSelectedLocationKey(event.target.value)}
                    >
                      {deliveryLocations.map((loc) => {
                        const key =
                          loc.id !== null && loc.id !== undefined
                            ? `id:${loc.id}`
                            : `name:${loc.name}`
                        return (
                          <option key={key} value={key}>
                            {loc.name} {loc.description ? `— ${loc.description}` : ''}
                          </option>
                        )
                      })}
                    </select>
                  </div>

                  <div className="field" style={{ margin: 0 }}>
                    <div className="field-label-row">
                      <label htmlFor="delivery-room">Room / Exact location details *</label>
                      <span className="field-hint">Max 255 chars</span>
                    </div>
                    <input
                      id="delivery-room"
                      type="text"
                      placeholder="e.g. Room 204, 2nd Floor, or Security Desk"
                      value={deliveryRoomDetails}
                      onChange={(event) => setDeliveryRoomDetails(event.target.value)}
                      maxLength={255}
                      required
                    />
                  </div>

                  <div className="field" style={{ margin: 0 }}>
                    <div className="field-label-row">
                      <label htmlFor="delivery-window">Preferred delivery window</label>
                      <span className="field-hint">Subject to partner availability</span>
                    </div>
                    <select
                      id="delivery-window"
                      value={deliveryTimeWindow}
                      onChange={(event) => setDeliveryTimeWindow(event.target.value)}
                    >
                      <option value="Standard (ASAP)">Standard (ASAP)</option>
                      <option value="Morning (9:00 AM - 12:00 PM)">Morning (9:00 AM - 12:00 PM)</option>
                      <option value="Afternoon (12:00 PM - 4:00 PM)">Afternoon (12:00 PM - 4:00 PM)</option>
                      <option value="Evening (4:00 PM - 7:00 PM)">Evening (4:00 PM - 7:00 PM)</option>
                    </select>
                  </div>
                </div>
              )}

              {pageCount !== null && (
                <div className="price-estimate">
                  <div className="price-estimate-heading">
                    <div>
                      <span className="price-estimate-label">
                        ESTIMATED TOTAL COST
                      </span>
                      <h3>₹{validEstimateCopies ? totalEstimatedPrice : '—'}</h3>
                    </div>
                    <span className="price-estimate-icon">₹</span>
                  </div>

                  <div className="price-estimate-details">
                    <div>
                      <span>PDF pages</span>
                      <strong>{pageCount}</strong>
                    </div>
                    <div>
                      <span>Physical sheets</span>
                      <strong>
                        {validEstimateCopies ? estimatedSheets : '—'}
                      </strong>
                    </div>
                    <div>
                      <span>Rate per sheet</span>
                      <strong>₹{ratePerSheet}</strong>
                    </div>
                    <div>
                      <span>Printing cost</span>
                      <strong>
                        ₹{validEstimateCopies ? estimatedPrintingCost : '—'}
                      </strong>
                    </div>
                    <div>
                      <span>Delivery method</span>
                      <strong>
                        {deliveryMethod === 'delivery' ? 'Campus delivery' : 'Self pickup'}
                      </strong>
                    </div>
                    <div>
                      <span>Delivery fee</span>
                      <strong>
                        {deliveryMethod === 'delivery' ? '₹5' : '₹0 (Free)'}
                      </strong>
                    </div>
                  </div>

                  <p>
                    {printSides === 'Double-sided'
                      ? 'Up to 2 PDF pages per sheet.'
                      : '1 PDF page per sheet.'}
                    {' '}Selected: {deliveryMethod === 'delivery' ? 'Campus delivery (+₹5 fee).' : 'Self pickup (₹0 fee).'} Final charges may be confirmed by the print shop.
                  </p>
                </div>
              )}

              {orderMessage && (
                <div
                  className={`form-message ${orderMessage.type === 'error' ? 'form-message-error' : 'form-message-success'}`}
                  role="status"
                >
                  <span>{orderMessage.type === 'error' ? '!' : '✓'}</span>
                  <p>{orderMessage.text}</p>
                </div>
              )}

              <button
                className="auth-submit place-order-button"
                type="submit"
                disabled={isSubmitting}
              >
                {isSubmitting ? 'Placing your order...' : 'Place print order'}
                {!isSubmitting && <span className="button-arrow">↗</span>}
              </button>

              <p className="privacy-note">
                <span>⌑</span> Your orders are connected to your student account.
              </p>
            </form>
          </article>

          <article className="surface-card history-card">
            <div className="section-heading history-heading">
              <div className="section-heading-icon history-heading-icon">▤</div>
              <div>
                <h2>Your order history</h2>
                <p>Track the status of your print requests.</p>
              </div>
            </div>

            {ordersStatus && orders.length === 0 ? (
              <div className="orders-state">
                <div className="orders-state-icon">◷</div>
                <strong>Your orders will appear here</strong>
                <p>{ordersStatus}</p>
              </div>
            ) : orders.length === 0 ? (
              <div className="orders-state">
                <div className="orders-state-icon">▤</div>
                <strong>No orders yet</strong>
                <p>Your first print order will appear here after you place it.</p>
              </div>
            ) : (
              <div className="orders-list">
                {orders.map((order) => (
                  <OrderCard key={order.id} order={order} />
                ))}
              </div>
            )}

            <div className="history-footnote">
              <span className="history-footnote-icon">↻</span>
              <p>Order statuses refresh automatically while this page is open.</p>
            </div>
          </article>
        </section>

        <footer className="dashboard-footer">
          <Brand />
          <p>Made for students, one print at a time.</p>
          <span>© {CURRENT_YEAR} XeroxQueue</span>
        </footer>
      </main>
    </div>
  )
}

function getErrorMessage(data, fallback) {
  const detail = data?.detail

  if (typeof detail === 'string') return detail

  if (Array.isArray(detail)) {
    return detail.map((item) => item.msg || 'Invalid input').join('. ')
  }

  return fallback
}

function App() {
  const [authView, setAuthView] = useState('login')
  const [portalView, setPortalView] = useState(() => {
    const saved = getStoredItem(STORAGE_KEYS.PORTAL_VIEW)
    return saved === 'admin' || saved === 'partner' ? saved : 'student'
  })
  const [student, setStudent] = useState(() => getStoredJson(STORAGE_KEYS.STUDENT_USER))
  const [accessToken, setAccessToken] = useState(() => getStoredItem(STORAGE_KEYS.STUDENT_TOKEN))
  const [authLoading, setAuthLoading] = useState(false)
  const [authMessage, setAuthMessage] = useState(null)

  function switchPortalView(view) {
    setPortalView(view)
    setStoredItem(STORAGE_KEYS.PORTAL_VIEW, view)
  }

  async function handleSignup(formData) {
    setAuthLoading(true)
    setAuthMessage(null)

    try {
      const response = await fetch(`${API}/api/auth/signup`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(formData),
      })

      const data = await response.json()

      if (!response.ok) {
        throw new Error(getErrorMessage(data, 'Unable to create your account.'))
      }

      setAuthView('login')
      setAuthMessage({
        type: 'success',
        text: data.message || 'Registration successful. Please sign in.',
      })
    } catch (error) {
      setAuthMessage({
        type: 'error',
        text: error.message || 'Could not connect to the server.',
      })
    } finally {
      setAuthLoading(false)
    }
  }

  async function handleLogin(formData) {
    setAuthLoading(true)
    setAuthMessage(null)

    try {
      const response = await fetch(`${API}/api/auth/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(formData),
      })

      const data = await response.json()

      if (!response.ok) {
        throw new Error(getErrorMessage(data, 'Unable to sign in.'))
      }

      if (!data.access_token || !data.student) {
        throw new Error('The server returned an incomplete login response.')
      }

      setStoredItem(STORAGE_KEYS.STUDENT_TOKEN, data.access_token)
      setStoredJson(STORAGE_KEYS.STUDENT_USER, data.student)
      clearConflictingSessions('student')
      setAccessToken(data.access_token)
      setStudent(data.student)
      setAuthMessage(null)
    } catch (error) {
      setAuthMessage({
        type: 'error',
        text: error.message || 'Could not connect to the server.',
      })
    } finally {
      setAuthLoading(false)
    }
  }

  function handleLogout(message = 'You have signed out successfully.') {
    clearRoleSession('student')
    setAccessToken('')
    setStudent(null)
    setAuthView('login')
    setAuthMessage({
      type: message.includes('expired') ? 'error' : 'success',
      text: message,
    })
  }

  if (portalView === 'admin') {
    return (
      <AdminDashboard
        onBack={() => switchPortalView('student')}
      />
    )
  }

  if (portalView === 'partner') {
    return (
      <PartnerPortal
        onBack={() => switchPortalView('student')}
      />
    )
  }

  if (!student || !accessToken) {
    return (
      <AuthPage
        authView={authView}
        setAuthView={setAuthView}
        onLogin={handleLogin}
        onSignup={handleSignup}
        onAdminClick={() => switchPortalView('admin')}
        onPartnerClick={() => switchPortalView('partner')}
        loading={authLoading}
        message={authMessage}
        setMessage={setAuthMessage}
      />
    )
  }

  return (
    <StudentDashboard
      student={student}
      accessToken={accessToken}
      onLogout={handleLogout}
    />
  )
}

export default App
