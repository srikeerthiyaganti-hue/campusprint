
import { useCallback, useEffect, useState } from 'react'
import './AdminDashboard.css'
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

const STATUSES = [
  'pending',
  'accepted',
  'printing',
  'ready',
  'completed',
  'cancelled',
]

const CURRENT_YEAR = new Date().getFullYear()

function getError(data, fallback) {
  if (typeof data?.detail === 'string') return data.detail
  if (Array.isArray(data?.detail)) {
    return data.detail.map((item) => item.msg || 'Invalid input').join('. ')
  }
  return fallback
}

function AdminBrand() {
  return (
    <div className="admin-brand">
      <div className="admin-brand-icon">Q<span>.</span></div>
      <div>
        <strong>XeroxQueue</strong>
        <small>PRINT MANAGEMENT</small>
      </div>
    </div>
  )
}

function StatusBadge({ status }) {
  return (
    <span className={`admin-status status-${status}`}>
      <span />
      {String(status || 'pending').replace(/_/g, ' ')}
    </span>
  )
}

function FulfillmentBadge({ type }) {
  const isDelivery = type === 'delivery'
  return (
    <span className={`admin-fulfillment-badge ${isDelivery ? 'fulfillment-delivery' : 'fulfillment-pickup'}`}>
      <span className="fulfillment-icon">{isDelivery ? '🚴' : '📦'}</span>
      {isDelivery ? 'Campus Delivery' : 'Self Pickup'}
    </span>
  )
}

function getAdminDeliveryStatus(order) {
  if (order.fulfillment_type !== 'delivery') return null

  const status = order.delivery_job_status
  switch (status) {
    case 'assigned':
      return {
        key: 'assigned',
        label: 'Partner Assigned',
        badgeClass: 'status-delivery-assigned',
      }
    case 'picked_up':
      return {
        key: 'picked_up',
        label: 'Picked Up',
        badgeClass: 'status-delivery-picked_up',
      }
    case 'delivered':
      return {
        key: 'delivered',
        label: 'Delivered',
        badgeClass: 'status-delivery-delivered',
      }
    case 'cancelled':
      return {
        key: 'cancelled',
        label: 'Delivery Cancelled',
        badgeClass: 'status-delivery-cancelled',
      }
    case 'available':
    default:
      return {
        key: 'available',
        label: 'Awaiting delivery partner',
        badgeClass: 'status-delivery-available',
      }
  }
}

function DeliveryStatusBadge({ statusInfo }) {
  if (!statusInfo) return null

  return (
    <span className={`admin-delivery-status ${statusInfo.badgeClass}`}>
      <span className="admin-delivery-dot" />
      {statusInfo.label}
    </span>
  )
}

function getEmptyOrdersMessage(fulfillmentFilter, filter, search, ordersCount) {
  if (ordersCount === 0) {
    return {
      title: 'No orders yet',
      desc: 'New student print requests will appear here as they are submitted.',
    }
  }

  if (search.trim()) {
    return {
      title: `No orders matching "${search.trim()}"`,
      desc: 'Try searching with a different student name, order ID, or delivery partner name.',
    }
  }

  if (fulfillmentFilter === 'delivery_active') {
    return {
      title: 'No active campus deliveries',
      desc:
        filter !== 'all'
          ? `No active campus deliveries currently match the "${filter.replace(/_/g, ' ')}" print status.`
          : 'There are no campus delivery orders currently awaiting partners or in transit.',
    }
  }

  if (fulfillmentFilter === 'delivery_delivered') {
    return {
      title: 'No delivered campus orders',
      desc:
        filter !== 'all'
          ? `No delivered campus orders currently match the "${filter.replace(/_/g, ' ')}" print status.`
          : 'No campus delivery orders have reached delivered status yet.',
    }
  }

  if (fulfillmentFilter === 'pickup') {
    return {
      title: 'No self-pickup orders found',
      desc:
        filter !== 'all'
          ? `No self-pickup orders match the "${filter.replace(/_/g, ' ')}" print status.`
          : 'There are currently no self-pickup print orders.',
    }
  }

  if (filter !== 'all') {
    return {
      title: `No orders with status "${filter.replace(/_/g, ' ')}"`,
      desc: 'Select "All statuses" or choose another status filter to see other orders.',
    }
  }

  return {
    title: 'No matching orders',
    desc: 'No print orders match the selected filters.',
  }
}

function AdminLogin({ onLogin, onBack }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [loading, setLoading] = useState(false)
  const [message, setMessage] = useState('')

  async function submit(event) {
    event.preventDefault()
    setLoading(true)
    setMessage('')

    try {
      await onLogin(username.trim(), password)
    } catch (error) {
      setMessage(error.message || 'Unable to sign in.')
    } finally {
      setLoading(false)
    }
  }

  return (
    <main className="admin-login-page">
      <section className="admin-login-art">
        <AdminBrand />

        <div className="admin-art-copy">
          <span className="admin-eyebrow">ADMINISTRATION PORTAL</span>
          <h1>
            Every order.
            <br />
            <span>Under control.</span>
          </h1>
          <p>
            Manage campus printing in one place. Review incoming requests,
            monitor progress, and keep every print job moving.
          </p>

          <div className="admin-art-points">
            <div><span>✓</span><div><strong>Live order overview</strong><small>See incoming student requests</small></div></div>
            <div><span>✓</span><div><strong>Simple status management</strong><small>Update jobs as they progress</small></div></div>
            <div><span>✓</span><div><strong>Protected admin access</strong><small>Separate from student accounts</small></div></div>
          </div>
        </div>

        <div className="admin-art-bottom">XeroxQueue · Campus printing, simplified</div>
      </section>

      <section className="admin-login-main">
        <div className="admin-login-card">
          <div className="admin-login-icon">⌑</div>
          <span className="admin-eyebrow">WELCOME BACK</span>
          <h2>Admin sign in</h2>
          <p className="admin-login-subtitle">
            Sign in to access the print shop dashboard.
          </p>

          {message && <div className="admin-alert admin-alert-error">{message}</div>}

          <form onSubmit={submit}>
            <label htmlFor="admin-username">Admin username</label>
            <input
              id="admin-username"
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              placeholder="Enter admin username"
              autoComplete="username"
              required
            />

            <label htmlFor="admin-password">Password</label>
            <div className="admin-password-field">
              <input
                id="admin-password"
                type={showPassword ? 'text' : 'password'}
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                placeholder="Enter your password"
                autoComplete="current-password"
                required
              />
              <button type="button" onClick={() => setShowPassword(!showPassword)}>
                {showPassword ? 'Hide' : 'Show'}
              </button>
            </div>

            <button className="admin-primary-button" disabled={loading}>
              {loading ? 'Signing in...' : 'Sign in to dashboard'}
              {!loading && <span>↗</span>}
            </button>
          </form>

          <div className="admin-login-note">
            <span>✓</span> Admin access is separate from student login.
          </div>
                    <button
            type="button"
            className="admin-back-button"
            onClick={onBack}
          >
            ← Back to Student Portal
          </button>
        </div>
        <footer className="admin-login-footer">© {CURRENT_YEAR} XeroxQueue</footer>
      </section>
    </main>
  )
}

export default function AdminDashboard({ onBack }) {
  const [token, setToken] = useState(() => getStoredItem(STORAGE_KEYS.ADMIN_TOKEN))
  const [admin, setAdmin] = useState(() => getStoredJson(STORAGE_KEYS.ADMIN_USER))
  const [orders, setOrders] = useState([])
  const [loading, setLoading] = useState(false)
  const [pageError, setPageError] = useState('')
  const [notice, setNotice] = useState('')
  const [filter, setFilter] = useState('all')
  const [fulfillmentFilter, setFulfillmentFilter] = useState('all') // 'all' | 'pickup' | 'delivery_active' | 'delivery_delivered'
  const [search, setSearch] = useState('')
  const [updatingId, setUpdatingId] = useState(null)

  // Delivery Partner State (Phase 2)
  const [activeTab, setActiveTab] = useState('orders') // 'orders' or 'partners'
  const [partners, setPartners] = useState([])
  const [partnersLoading, setPartnersLoading] = useState(false)
  const [partnerFilter, setPartnerFilter] = useState('all')
  const [partnerSearch, setPartnerSearch] = useState('')
  const [updatingPartnerId, setUpdatingPartnerId] = useState(null)
  const [partnerMigrationNotice, setPartnerMigrationNotice] = useState('')

  async function login(username, password) {
    const response = await fetch(`${API}/api/admin/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    })

    const data = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(getError(data, 'Admin login failed.'))

    if (!data.access_token) throw new Error('The server did not return an admin token.')

    const adminUser = data.admin || { username }
    clearConflictingSessions('admin')
    setToken(data.access_token)
    setAdmin(adminUser)
    setStoredItem(STORAGE_KEYS.ADMIN_TOKEN, data.access_token)
    setStoredJson(STORAGE_KEYS.ADMIN_USER, adminUser)
    setPageError('')
  }

  const loadOrders = useCallback(async () => {
    if (!token) return

    try {
      const response = await fetch(`${API}/api/admin/orders`, {
        headers: { Authorization: `Bearer ${token}` },
      })

      const data = await response.json().catch(() => ({}))

      if (response.status === 401 || response.status === 403) {
        clearRoleSession('admin')
        setPageError('Your admin session is invalid or expired. Please sign in again.')
        setToken('')
        setAdmin(null)
        setOrders([])
        setPartners([])
        return
      }

      if (!response.ok) throw new Error(getError(data, 'Could not load orders.'))

      setOrders(Array.isArray(data.orders) ? data.orders : [])
      setPageError('')
    } catch (error) {
      setPageError(error.message || 'Could not connect to the backend.')
    } finally {
      setLoading(false)
    }
  }, [token])

  const loadPartners = useCallback(async () => {
    if (!token) return

    try {
      const response = await fetch(`${API}/api/admin/partners`, {
        headers: { Authorization: `Bearer ${token}` },
      })

      const data = await response.json().catch(() => ({}))

      if (response.status === 401 || response.status === 403) {
        clearRoleSession('admin')
        setPageError('Your admin session is invalid or expired. Please sign in again.')
        setToken('')
        setAdmin(null)
        setOrders([])
        setPartners([])
        return
      }

      if (response.status === 503) {
        const msg =
          data?.detail?.message ||
          getError(data, 'Delivery partner database table does not exist. Migration 001_campus_delivery_system.sql is pending.')
        setPartnerMigrationNotice(msg)
        setPartners([])
        return
      }

      if (!response.ok) {
        throw new Error(getError(data, 'Could not load delivery partners.'))
      }

      setPartners(Array.isArray(data.partners) ? data.partners : [])
      setPartnerMigrationNotice('')
    } catch (error) {
      setPageError(error.message || 'Could not connect to the backend.')
    } finally {
      setPartnersLoading(false)
    }
  }, [token])

  useEffect(() => {
    if (!token) return

    let cancelled = false
    async function fetchData() {
      if (cancelled) return
      if (activeTab === 'orders') {
        await loadOrders()
      } else {
        await loadPartners()
      }
    }

    fetchData()

    const interval = window.setInterval(() => {
      if (!cancelled) {
        if (activeTab === 'orders') loadOrders()
        else loadPartners()
      }
    }, 10000)

    return () => {
      cancelled = true
      window.clearInterval(interval)
    }
  }, [token, activeTab, loadOrders, loadPartners])

  async function updateStatus(orderId, status) {
    setUpdatingId(orderId)
    setNotice('')
    setPageError('')

    try {
      const response = await fetch(`${API}/api/orders/${orderId}/status`, {
        method: 'PATCH',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({ status }),
      })

      const data = await response.json().catch(() => ({}))
      if (!response.ok) throw new Error(getError(data, 'Status update failed.'))

      setOrders((previous) =>
        previous.map((order) =>
          order.id === orderId ? { ...order, status } : order
        )
      )
      setNotice(`Order #${orderId} updated to ${status.replace(/_/g, ' ')}.`)
    } catch (error) {
      setPageError(error.message || 'Could not update order status.')
    } finally {
      setUpdatingId(null)
    }
  }

  async function updatePartnerApproval(partnerId, status, rejectionReason = null) {
    setUpdatingPartnerId(partnerId)
    setNotice('')
    setPageError('')

    try {
      const response = await fetch(`${API}/api/admin/partners/${partnerId}/approval`, {
        method: 'PATCH',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({
          status,
          rejection_reason: rejectionReason,
        }),
      })

      const data = await response.json().catch(() => ({}))

      if (response.status === 503) {
        throw new Error(
          data?.detail?.message || 'Database migration 001 is required before approving partners.'
        )
      }

      if (!response.ok) {
        throw new Error(getError(data, 'Could not update partner approval status.'))
      }

      setPartners((prev) =>
        prev.map((p) =>
          p.id === partnerId
            ? {
                ...p,
                approval_status: status,
                is_approved: status === 'approved' ? 1 : 0,
                approved_by: status === 'approved' ? admin?.username : p.approved_by,
              }
            : p
        )
      )
      setNotice(data.message || `Partner #${partnerId} status updated to ${status}.`)
    } catch (error) {
      setPageError(error.message || 'Could not update partner approval.')
    } finally {
      setUpdatingPartnerId(null)
    }
  }

  function logout() {
    clearRoleSession('admin')
    setToken('')
    setAdmin(null)
    setOrders([])
    setPartners([])
    setPartnerMigrationNotice('')
    setNotice('')
    setPageError('')
  }

  async function viewDocument(orderId) {
    const pdfTab = window.open('about:blank', '_blank')

    if (!pdfTab) {
      alert('Please allow pop-ups to view the PDF.')
      return
    }

    pdfTab.document.title = 'Loading PDF...'
    pdfTab.document.body.innerHTML =
      '<p style="font-family:Arial;padding:24px">Loading document...</p>'

    try {
      const response = await fetch(
        `${API}/api/admin/orders/${orderId}/document`,
        {
          headers: {
            Authorization: `Bearer ${token}`,
          },
        }
      )

      if (!response.ok) {
        throw new Error(
          response.status === 404
            ? 'The PDF was not found for this order.'
            : 'Could not open the PDF. Please sign in again if needed.'
        )
      }

      const pdfBlob = await response.blob()
      const pdfUrl = URL.createObjectURL(pdfBlob)

      pdfTab.location.href = pdfUrl

      // Keep the URL available while the PDF tab is open.
      pdfTab.addEventListener('pagehide', () => {
        URL.revokeObjectURL(pdfUrl)
      }, { once: true })
    } catch (error) {
      pdfTab.close()
      alert(error.message || 'Unable to open the document.')
    }
  }

  if (!token) {
    return <AdminLogin onLogin={login} onBack={onBack} />
  }

  const pending = orders.filter((o) => o.status === 'pending').length
  const active = orders.filter((o) => ['accepted', 'printing'].includes(o.status)).length
  const ready = orders.filter((o) => o.status === 'ready').length
  const completed = orders.filter((o) => o.status === 'completed').length

  const pickupOrders = orders.filter((o) => o.fulfillment_type !== 'delivery')
  const deliveryOrders = orders.filter((o) => o.fulfillment_type === 'delivery')
  const deliveryActiveOrders = deliveryOrders.filter((o) => {
    const s = o.delivery_job_status
    return s !== 'delivered' && s !== 'cancelled' && o.status !== 'cancelled'
  })
  const deliveryDeliveredOrders = deliveryOrders.filter(
    (o) => o.delivery_job_status === 'delivered'
  )

  const visibleOrders = orders.filter((order) => {
    // Fulfillment filter
    if (fulfillmentFilter === 'pickup' && order.fulfillment_type === 'delivery') {
      return false
    }
    if (fulfillmentFilter === 'delivery_active') {
      if (order.fulfillment_type !== 'delivery') return false
      const s = order.delivery_job_status
      if (s === 'delivered' || s === 'cancelled' || order.status === 'cancelled') return false
    }
    if (fulfillmentFilter === 'delivery_delivered') {
      if (order.fulfillment_type !== 'delivery') return false
      if (order.delivery_job_status !== 'delivered') return false
    }

    // Print status filter
    const matchesStatus = filter === 'all' || order.status === filter

    // Search query
    const term = search.trim().toLowerCase()
    const matchesSearch =
      !term ||
      [
        order.id,
        order.student_name,
        order.file_name,
        order.student_db_id,
        order.delivery_partner_name,
        order.delivery_partner_phone,
        order.delivery_location_name,
        order.delivery_room_details,
      ].some((value) => String(value ?? '').toLowerCase().includes(term))

    return matchesStatus && matchesSearch
  })

  const emptyInfo = getEmptyOrdersMessage(
    fulfillmentFilter,
    filter,
    search,
    orders.length
  )

  const pendingPartners = partners.filter((p) => p.approval_status === 'pending').length
  const approvedPartners = partners.filter((p) => p.approval_status === 'approved').length
  const rejectedPartners = partners.filter((p) => p.approval_status === 'rejected').length

  const visiblePartners = partners.filter((p) => {
    const matchesStatus = partnerFilter === 'all' || p.approval_status === partnerFilter
    const term = partnerSearch.trim().toLowerCase()
    const matchesSearch =
      !term ||
      [p.id, p.full_name, p.student_id, p.phone_number, p.email].some((val) =>
        String(val ?? '').toLowerCase().includes(term)
      )
    return matchesStatus && matchesSearch
  })

  return (
    <div className="admin-dashboard">
      <aside className="admin-sidebar">
        <AdminBrand />
        <div className="admin-sidebar-label">WORKSPACE</div>
        <button
          type="button"
          className={`admin-sidebar-link ${activeTab === 'orders' ? 'active' : ''}`}
          onClick={() => setActiveTab('orders')}
        >
          <span>▦</span> Order overview
        </button>
        <button
          type="button"
          className={`admin-sidebar-link ${activeTab === 'partners' ? 'active' : ''}`}
          onClick={() => {
            setActiveTab('partners')
            loadPartners()
          }}
        >
          <span>🚴</span> Delivery partners
        </button>
        <div className="admin-sidebar-bottom">
          <div className="admin-avatar">{(admin?.username || 'A').charAt(0).toUpperCase()}</div>
          <div><strong>{admin?.username || 'Administrator'}</strong><small>Shop administrator</small></div>
          <button onClick={logout} title="Sign out">↗</button>
        </div>
      </aside>

      <main className="admin-workspace">
        <header className="admin-topbar">
          <div>
            <span className="admin-breadcrumb">
              Workspace / <strong>{activeTab === 'orders' ? 'Overview' : 'Delivery Partners'}</strong>
            </span>
            <h1>{activeTab === 'orders' ? 'Print shop overview' : 'Delivery partner management'}</h1>
          </div>
          <div className="admin-topbar-actions">
            <span className="admin-live"><i /> Live updates · 10 sec</span>
            <button
              className="admin-refresh"
              onClick={() => {
                if (activeTab === 'orders') {
                  setLoading(true)
                  loadOrders()
                } else {
                  loadPartners()
                }
              }}
            >
              ↻ Refresh
            </button>
          </div>
        </header>

        {pageError && <div className="admin-alert admin-alert-error">{pageError}</div>}
        {notice && <div className="admin-alert admin-alert-success">{notice}</div>}

        {activeTab === 'orders' ? (
          <>
            <section className="admin-welcome">
              <div>
                <span className="admin-eyebrow">YOUR PRINT OPERATIONS</span>
                <h2>Good work starts here.</h2>
                <p>Manage incoming student requests and keep the queue moving.</p>
              </div>
              <div className="admin-welcome-mark">Q<span>.</span></div>
            </section>

            <section className="admin-stats">
              <article><span className="admin-stat-icon blue">▦</span><div><small>Total orders</small><strong>{orders.length}</strong><span>All student requests</span></div></article>
              <article><span className="admin-stat-icon amber">◷</span><div><small>Pending</small><strong>{pending}</strong><span>Awaiting acceptance</span></div></article>
              <article><span className="admin-stat-icon purple">↻</span><div><small>In progress</small><strong>{active}</strong><span>Accepted or printing</span></div></article>
              <article><span className="admin-stat-icon teal">✓</span><div><small>Ready to collect</small><strong>{ready}</strong><span>Completed: {completed}</span></div></article>
            </section>

            <section className="admin-orders-panel">
              <div className="admin-orders-heading">
                <div><h2>Incoming orders</h2><p>Review requests and update their printing status.</p></div>
                <span className="admin-order-count">{visibleOrders.length} orders</span>
              </div>

              <div className="admin-filter-bar" role="tablist" aria-label="Filter orders by fulfillment">
                <button
                  type="button"
                  className={`admin-filter-btn ${fulfillmentFilter === 'all' ? 'active' : ''}`}
                  onClick={() => setFulfillmentFilter('all')}
                >
                  All Orders ({orders.length})
                </button>
                <button
                  type="button"
                  className={`admin-filter-btn ${fulfillmentFilter === 'pickup' ? 'active' : ''}`}
                  onClick={() => setFulfillmentFilter('pickup')}
                >
                  Self Pickup ({pickupOrders.length})
                </button>
                <button
                  type="button"
                  className={`admin-filter-btn ${fulfillmentFilter === 'delivery_active' ? 'active' : ''}`}
                  onClick={() => setFulfillmentFilter('delivery_active')}
                >
                  Campus Delivery — Active ({deliveryActiveOrders.length})
                </button>
                <button
                  type="button"
                  className={`admin-filter-btn ${fulfillmentFilter === 'delivery_delivered' ? 'active' : ''}`}
                  onClick={() => setFulfillmentFilter('delivery_delivered')}
                >
                  Campus Delivery — Delivered ({deliveryDeliveredOrders.length})
                </button>
              </div>

              <div className="admin-order-tools">
                <input
                  aria-label="Search orders"
                  placeholder="Search student, file, order #, partner, or location..."
                  value={search}
                  onChange={(event) => setSearch(event.target.value)}
                />
                <select aria-label="Filter by status" value={filter} onChange={(event) => setFilter(event.target.value)}>
                  <option value="all">All statuses</option>
                  {STATUSES.map((status) => <option key={status} value={status}>{status.replace(/_/g, ' ')}</option>)}
                </select>
              </div>

              {loading && orders.length === 0 ? (
                <div className="admin-empty"><span>◷</span><strong>Loading orders...</strong><p>Fetching the latest print requests.</p></div>
              ) : visibleOrders.length === 0 ? (
                <div className="admin-empty">
                  <span>{fulfillmentFilter.startsWith('delivery') ? '🚴' : '▤'}</span>
                  <strong>{emptyInfo.title}</strong>
                  <p>{emptyInfo.desc}</p>
                </div>
              ) : (
                <div className="admin-table-wrap">
                  <table className="admin-orders-table">
                    <thead>
                      <tr>
                        <th>ORDER / DOCUMENT</th>
                        <th>STUDENT</th>
                        <th>PRINT SETTINGS</th>
                        <th>FULFILLMENT & DELIVERY</th>
                        <th>DATE</th>
                        <th>PRINT STATUS</th>
                        <th>UPDATE STATUS</th>
                      </tr>
                    </thead>
                    <tbody>
                      {visibleOrders.map((order) => (
                        <tr key={order.id}>
                          <td>
                            <strong>#{order.id}</strong>
                            <span className="admin-document-name">
                              {order.file_name || 'PDF document'}
                            </span>
                            {order.stored_file_name ? (
                              <button
                                type="button"
                                className="admin-view-pdf-button"
                                onClick={() => viewDocument(order.id)}
                              >
                                ↗ View PDF
                              </button>
                            ) : (
                              <small className="admin-muted">PDF unavailable</small>
                            )}
                          </td>

                          <td><strong>{order.student_name || 'Student'}</strong><span className="admin-muted">ID: {order.student_db_id ?? '—'}</span></td>
                          <td>
                            <span>{order.copies} {Number(order.copies) === 1 ? 'copy' : 'copies'}</span>
                            <span className="admin-muted">
                              {order.print_type === 'black_white' ? 'Black & white' : 'Colour'}
                              {' · '}
                              {order.print_sides === 'single' ? 'Single-sided' : 'Double-sided'}
                            </span>
                            <span className="admin-muted">
                              {order.page_count != null
                                ? `${order.page_count} ${Number(order.page_count) === 1 ? 'page' : 'pages'} · ${order.physical_sheets} ${Number(order.physical_sheets) === 1 ? 'sheet' : 'sheets'} total`
                                : 'Page count unavailable'}
                            </span>
                            <span className="admin-estimated-price">
                              {order.estimated_price != null
                                ? `Estimated: ₹${Number(order.estimated_price).toFixed(2)}`
                                : 'Estimate unavailable'}
                            </span>
                          </td>

                          <td className="admin-fulfillment-cell">
                            <div className="admin-delivery-cell-inner">
                              <div className="admin-fulfillment-row">
                                <FulfillmentBadge type={order.fulfillment_type} />
                                {order.fulfillment_type === 'delivery' && (
                                  <DeliveryStatusBadge statusInfo={getAdminDeliveryStatus(order)} />
                                )}
                              </div>

                              {order.fulfillment_type === 'delivery' ? (
                                <>
                                  {order.delivery_partner_name ? (
                                    <div className="admin-delivery-partner-info">
                                      <span className="admin-partner-label">Partner:</span>
                                      <strong className="admin-partner-name">{order.delivery_partner_name}</strong>
                                      {order.delivery_partner_phone && (
                                        <span className="admin-partner-phone">({order.delivery_partner_phone})</span>
                                      )}
                                    </div>
                                  ) : order.delivery_partner_id ? (
                                    <div className="admin-delivery-partner-info">
                                      <span className="admin-partner-label">Partner:</span>
                                      <span className="admin-muted">ID #{order.delivery_partner_id}</span>
                                    </div>
                                  ) : (
                                    <small className="admin-muted">No partner assigned</small>
                                  )}

                                  <div className="admin-delivery-meta">
                                    <span className="admin-fee-tag">
                                      Fee: ₹{order.delivery_fee != null ? Number(order.delivery_fee).toFixed(0) : '5'}
                                    </span>
                                    {(order.delivery_location_name || order.delivery_room_details) && (
                                      <span
                                        className="admin-location-tag"
                                        title={`${order.delivery_location_name || ''} ${order.delivery_room_details || ''}`}
                                      >
                                        📍 {order.delivery_location_name || 'Campus'}
                                        {order.delivery_room_details ? ` (${order.delivery_room_details})` : ''}
                                      </span>
                                    )}
                                  </div>
                                </>
                              ) : (
                                <small className="admin-muted">In-shop pickup (₹0 fee)</small>
                              )}
                            </div>
                          </td>

                          <td>{order.created_at ? new Date(order.created_at).toLocaleString() : '—'}</td>
                          <td><StatusBadge status={order.status} /></td>
                          <td>
                            <select
                              aria-label={`Update order ${order.id} status`}
                              value={order.status}
                              disabled={updatingId === order.id}
                              onChange={(event) => updateStatus(order.id, event.target.value)}
                            >
                              {STATUSES.map((status) => <option key={status} value={status}>{status.replace(/_/g, ' ')}</option>)}
                            </select>
                            {updatingId === order.id && <small className="admin-saving">Saving...</small>}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              <div className="admin-panel-footer">
                <span>{orders.length} total database orders</span>
                <span>Refreshes automatically every 10 seconds</span>
              </div>
            </section>
          </>
        ) : (
          <>
            <section className="admin-welcome">
              <div>
                <span className="admin-eyebrow">CAMPUS DELIVERY PARTNERS</span>
                <h2>Delivery partner vetting & approvals</h2>
                <p>Review student applications, verify college enrollment, and manage delivery partner privileges.</p>
              </div>
              <div className="admin-welcome-mark">🚴</div>
            </section>

            <section className="admin-stats">
              <article>
                <span className="admin-stat-icon blue">🚴</span>
                <div>
                  <small>Total applicants</small>
                  <strong>{partners.length}</strong>
                  <span>Registered students</span>
                </div>
              </article>
              <article>
                <span className="admin-stat-icon amber">◷</span>
                <div>
                  <small>Pending review</small>
                  <strong>{pendingPartners}</strong>
                  <span>Awaiting decision</span>
                </div>
              </article>
              <article>
                <span className="admin-stat-icon teal">✓</span>
                <div>
                  <small>Approved active</small>
                  <strong>{approvedPartners}</strong>
                  <span>Authorized to deliver</span>
                </div>
              </article>
              <article>
                <span className="admin-stat-icon purple">✕</span>
                <div>
                  <small>Rejected / Revoked</small>
                  <strong>{rejectedPartners}</strong>
                  <span>Access restricted</span>
                </div>
              </article>
            </section>

            {partnerMigrationNotice ? (
              <div className="partner-migration-card">
                <h3>⚠️ Database Migration Prerequisite: Migration 001</h3>
                <p>
                  The <code>delivery_partners</code> table has not been created in <code>xeroxqueue_db</code> yet.
                  Delivery partner applications and approvals require applying <code>backend/migrations/001_campus_delivery_system.sql</code>.
                </p>
                <p><strong>Database response:</strong> {partnerMigrationNotice}</p>
                <small>Per safety requirements, migrations must be executed intentionally with a verified database backup.</small>
              </div>
            ) : (
              <section className="admin-orders-panel">
                <div className="admin-orders-heading">
                  <div>
                    <h2>Partner applications</h2>
                    <p>Approve, reject, or revoke delivery partner privileges.</p>
                  </div>
                  <span className="admin-order-count">{visiblePartners.length} applicants</span>
                </div>

                <div className="admin-order-tools">
                  <input
                    aria-label="Search partners"
                    placeholder="Search applicant name, student ID, phone, or email..."
                    value={partnerSearch}
                    onChange={(event) => setPartnerSearch(event.target.value)}
                  />
                  <select
                    aria-label="Filter partners by status"
                    value={partnerFilter}
                    onChange={(event) => setPartnerFilter(event.target.value)}
                  >
                    <option value="all">All statuses</option>
                    <option value="pending">Pending</option>
                    <option value="approved">Approved</option>
                    <option value="rejected">Rejected</option>
                  </select>
                </div>

                {partnersLoading && partners.length === 0 ? (
                  <div className="admin-empty">
                    <span>◷</span>
                    <strong>Loading partner applications...</strong>
                    <p>Fetching student applicant records.</p>
                  </div>
                ) : visiblePartners.length === 0 ? (
                  <div className="admin-empty">
                    <span>🚴</span>
                    <strong>No matching delivery partners</strong>
                    <p>New student delivery partner applications will appear here.</p>
                  </div>
                ) : (
                  <div className="admin-table-wrap">
                    <table className="admin-orders-table">
                      <thead>
                        <tr>
                          <th>APPLICANT</th>
                          <th>STUDENT ID</th>
                          <th>PHONE NUMBER</th>
                          <th>APPLIED ON</th>
                          <th>STATUS</th>
                          <th>APPROVAL ACTION</th>
                        </tr>
                      </thead>
                      <tbody>
                        {visiblePartners.map((p) => (
                          <tr key={p.id}>
                            <td>
                              <strong>{p.full_name}</strong>
                              <span className="admin-document-name">{p.email}</span>
                            </td>
                            <td>
                              <strong>{p.student_id}</strong>
                              {p.student_db_id && <small className="admin-muted">Linked student #{p.student_db_id}</small>}
                            </td>
                            <td>
                              <span>{p.phone_number}</span>
                            </td>
                            <td>
                              {p.created_at ? new Date(p.created_at).toLocaleDateString() : '—'}
                              {p.approved_by && <small className="admin-muted">Approved by {p.approved_by}</small>}
                              {p.rejected_by && <small className="admin-muted">Rejected by {p.rejected_by}</small>}
                            </td>
                            <td>
                              <StatusBadge status={p.approval_status || 'pending'} />
                            </td>
                            <td>
                              <div className="admin-partner-actions">
                                {p.approval_status === 'pending' && (
                                  <>
                                    <button
                                      type="button"
                                      className="admin-btn-approve"
                                      disabled={updatingPartnerId === p.id}
                                      onClick={() => updatePartnerApproval(p.id, 'approved')}
                                    >
                                      {updatingPartnerId === p.id ? '...' : '✓ Approve'}
                                    </button>
                                    <button
                                      type="button"
                                      className="admin-btn-reject"
                                      disabled={updatingPartnerId === p.id}
                                      onClick={() => updatePartnerApproval(p.id, 'rejected')}
                                    >
                                      {updatingPartnerId === p.id ? '...' : '✕ Reject'}
                                    </button>
                                  </>
                                )}

                                {p.approval_status === 'approved' && (
                                  <button
                                    type="button"
                                    className="admin-btn-revoke"
                                    disabled={updatingPartnerId === p.id}
                                    onClick={() => updatePartnerApproval(p.id, 'rejected')}
                                  >
                                    {updatingPartnerId === p.id ? 'Updating...' : 'Revoke Approval'}
                                  </button>
                                )}

                                {p.approval_status === 'rejected' && (
                                  <button
                                    type="button"
                                    className="admin-btn-approve"
                                    disabled={updatingPartnerId === p.id}
                                    onClick={() => updatePartnerApproval(p.id, 'approved')}
                                  >
                                    {updatingPartnerId === p.id ? 'Updating...' : 'Re-approve'}
                                  </button>
                                )}
                              </div>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
                <div className="admin-panel-footer">
                  <span>{partners.length} total applicant records</span>
                  <span>Refreshes automatically every 10 seconds</span>
                </div>
              </section>
            )}
          </>
        )}

        <footer className="admin-dashboard-footer">
          <AdminBrand /><span>Made for campus printing.</span><span>© {CURRENT_YEAR} XeroxQueue</span>
        </footer>
      </main>
    </div>
  )
}
