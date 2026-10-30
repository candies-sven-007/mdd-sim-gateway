// Thin REST + WebSocket client for the manager API (same origin).
const base = ''
let csrfToken = ''

export function setCsrf(token) { csrfToken = token || '' }

async function j(method, path, body) {
  const opt = { method, headers: {} }
  if (csrfToken && !['GET', 'HEAD', 'OPTIONS'].includes(method)) opt.headers['X-MDD-CSRF-Token'] = csrfToken
  if (body !== undefined) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body) }
  const r = await fetch(base + path, opt)
  const text = await r.text()
  let data
  try { data = text ? JSON.parse(text) : {} } catch { data = { raw: text } }
  // A non-empty CSRF token means this tab previously had an authenticated session.
  // Sessions are intentionally memory-only and disappear when the control plane restarts;
  // notify the app once so it can stop all polling and return to the login screen.
  if (r.status === 401 && csrfToken) {
    csrfToken = ''
    window.dispatchEvent(new CustomEvent('mdd-auth-expired'))
  }
  // detail may be a structured dict (e.g. {code, message}); prefer its message so
  // alerts show readable text instead of "[object Object]".
  const detailMsg = data.detail && typeof data.detail === 'object' ? (data.detail.message || data.detail.code) : data.detail
  if (!r.ok) throw Object.assign(new Error(detailMsg || data.error || r.statusText), { status: r.status, data })
  return data
}

// Multipart form submit (file uploads). Mirrors j()'s CSRF/401/error handling, but must not
// set a Content-Type header itself -- the browser needs to add the multipart boundary.
async function form(method, path, formData) {
  const opt = { method, headers: {}, body: formData }
  if (csrfToken && !['GET', 'HEAD', 'OPTIONS'].includes(method)) opt.headers['X-MDD-CSRF-Token'] = csrfToken
  const r = await fetch(base + path, opt)
  const text = await r.text()
  let data
  try { data = text ? JSON.parse(text) : {} } catch { data = { raw: text } }
  if (r.status === 401 && csrfToken) {
    csrfToken = ''
    window.dispatchEvent(new CustomEvent('mdd-auth-expired'))
  }
  const detailMsg = data.detail && typeof data.detail === 'object' ? (data.detail.message || data.detail.code) : data.detail
  if (!r.ok) throw Object.assign(new Error(detailMsg || data.error || r.statusText), { status: r.status, data })
  return data
}

/** Build query string. Prefer reader NAME (stable); index is optional fallback. */
function readerQuery(readerOrIndex, maybeName) {
  const q = new URLSearchParams()
  if (typeof readerOrIndex === 'string' && readerOrIndex) {
    q.set('reader', readerOrIndex)
  } else if (typeof readerOrIndex === 'number') {
    q.set('reader_index', String(readerOrIndex))
    if (maybeName) q.set('reader', maybeName)
  } else if (maybeName) {
    q.set('reader', maybeName)
  } else {
    q.set('reader_index', '0')
  }
  return q
}

function readerBody(readerOrIndex, extra = {}) {
  if (typeof readerOrIndex === 'string' && readerOrIndex) {
    return { reader: readerOrIndex, ...extra }
  }
  if (typeof readerOrIndex === 'number') {
    return { reader_index: readerOrIndex, ...extra }
  }
  if (readerOrIndex && typeof readerOrIndex === 'object') {
    return { ...readerOrIndex, ...extra }
  }
  return { reader_index: 0, ...extra }
}

export const api = {
  authStatus: () => j('GET', '/api/auth/status'),
  authSetup: (username, password, remember) => j('POST', '/api/auth/setup', { username, password, remember }),
  authLogin: (username, password, remember) => j('POST', '/api/auth/login', { username, password, remember }),
  authLogout: () => j('POST', '/api/auth/logout', {}),
  authPassword: (current_password, new_password) => j('POST', '/api/auth/password', { current_password, new_password }),
  // Unified physical-device control plane. Older deployments may return 404;
  // App.jsx then derives read-only device cards from /api/instances + /api/cards.
  devices: () => j('GET', '/api/devices'),
  patchDeviceCapabilities: (id, patch) => j('PATCH', `/api/devices/${encodeURIComponent(id)}/capabilities`, patch),
  deviceDiagnostics: (id) => j('POST', `/api/devices/${encodeURIComponent(id)}/diagnostics`, {}),
  saveDeviceHardware: (id, patch) => j('PUT', `/api/devices/${encodeURIComponent(id)}/hardware`, patch),
  saveDeviceName: (id, display_name) => j('PUT', `/api/devices/${encodeURIComponent(id)}/name`, { display_name }),
  rereadDeviceSim: (id) => j('POST', `/api/devices/${encodeURIComponent(id)}/sim/reread`),
  deleteDevice: (id) => j('DELETE', `/api/devices/${encodeURIComponent(id)}`),
  // Modem-like USB devices no configured model matches, tested only when asked.
  readers: () => j('GET', '/api/readers'),
  detect: (i = 0) => j('GET', `/api/sim/detect?reader_index=${i}`),
  // `reader` (PC/SC reader NAME) lets the backend re-resolve the index at request time —
  // indices shift when another reader is unplugged, and a stale index could address the
  // wrong physical SIM.
  verifyPin: (pin, reader_index = 0, reader, reader_port) => j('POST', '/api/sim/verify-pin', { pin, reader_index, reader, reader_port }),
  changePin: (oldp, newp, reader_index = 0, reader, reader_port) => j('POST', '/api/sim/change-pin', { old: oldp, new: newp, reader_index, reader, reader_port }),
  setPinEnabled: (pin, enabled, reader_index = 0, reader, reader_port) => j('POST', '/api/sim/pin-enabled', { pin, enabled, reader_index, reader, reader_port }),

  settings: () => j('GET', '/api/settings'),
  saveSettings: (patch) => j('PUT', '/api/settings', patch),
  egressStatus: () => j('GET', '/api/egress/status'),
  testEgress: (country) => j('POST', `/api/egress/${encodeURIComponent(country)}/test`, {}),
  testProxyProfile: (profileId, profile) => j('POST', `/api/egress/profile/${encodeURIComponent(profileId)}/test`, profile || {}),
  refreshEgress: () => j('POST', '/api/egress/refresh', {}),
  systemStatus: () => j('GET', '/api/system/status'),
  clearHostAlerts: () => j('DELETE', '/api/system/host-alerts'),
  createBackup: () => j('POST', '/api/system/backups', {}),
  deleteBackup: (name) => j('DELETE', `/api/system/backups/${encodeURIComponent(name)}`),
  maintenance: (action) => j('POST', '/api/system/maintenance', { action }),
  restartProgress: () => j('GET', '/api/system/maintenance/restart-progress'),
  supportBundleUrl: '/api/diagnostics/support-bundle',

  instances: () => j('GET', '/api/instances'),
  cards: () => j('GET', '/api/cards'),
  portsSuggest: () => j('GET', '/api/ports/suggest'),
  provision: (body) => j('POST', '/api/provision', body),
  saveInstance: (inst) => j('POST', '/api/instances', inst),
  setLineCountry: (id, country) => j('PUT', `/api/instances/${id}/country`, { country }),
  deleteInstance: (id, deleteHistory = true) => j('DELETE', `/api/instances/${id}?delete_history=${deleteHistory ? 'true' : 'false'}&confirm_id=${encodeURIComponent(id)}`),
  start: (id, body) => j('POST', `/api/instances/${id}/start`, body || {}),
  stop: (id) => j('POST', `/api/instances/${id}/stop`),
  reprovision: (id, body) => j('POST', `/api/instances/${id}/reprovision`, body || {}),
  clearPin: (id) => j('POST', `/api/instances/${id}/pin/clear`),
  status: (id) => j('GET', `/api/instances/${id}/status`),
  // Recorded VoWiFi up/down timeline; the window follows the accumulated history (max 2 days).
  lineAvailability: (id) => j('GET', `/api/instances/${id}/availability`),
  logs: (id, tail = 300) => j('GET', `/api/instances/${id}/logs?tail=${tail}`),
  register: (id) => j('POST', `/api/instances/${id}/register`),

  calls: (id) => j('GET', `/api/instances/${id}/calls`),
  // delete call-log entries: { ids:[...] } | { all:true }
  deleteCalls: (id, sel) => j('POST', `/api/instances/${id}/calls/delete`, sel),
  call: (id, to, from_endpoint = 'webrtc') => j('POST', `/api/instances/${id}/call`, { to, from_endpoint }),
  hangup: (id) => j('POST', `/api/instances/${id}/hangup`),
  softphone: (id) => j('GET', `/api/instances/${id}/softphone`),
  // Received texts (control/app/routers/sms.py); receive only.
  smsList: (instance = '') => j('GET', `/api/sms${instance ? `?instance=${encodeURIComponent(instance)}` : ''}`),
  smsRead: (body) => j('POST', '/api/sms/read', body || {}),
  smsDelete: (id) => j('DELETE', `/api/sms/${encodeURIComponent(id)}`),
  smsClear: (instance = '') => j('DELETE', `/api/sms${instance ? `?instance=${encodeURIComponent(instance)}` : ''}`),
  // Bluetooth speaker (control/app/routers/bluetooth.py). The MAC is validated server-side too.
  bluetoothStatus: () => j('GET', '/api/bluetooth/status'),
  bluetoothScan: () => j('POST', '/api/bluetooth/scan'),
  bluetoothDevice: (mac, action) => j('POST', `/api/bluetooth/devices/${encodeURIComponent(mac)}/${encodeURIComponent(action)}`),
  bluetoothVolume: (body) => j('PUT', '/api/bluetooth/volume', body),
  bluetoothAutoconnect: (mac) => j('PUT', '/api/bluetooth/autoconnect', { mac }),
  // Call media mode; only direct remains.
  media: () => j('GET', '/api/media'),

  // eSIM / LPA (lpac) — first arg is usually the PC/SC reader NAME (string).
  // Optional se_id / aid target a specific Secure Element on dual-SE cards.
  esimStatus: () => j('GET', '/api/esim/status'),
  esimChip: (readerOrIndex, maybeName) => j('GET', `/api/esim/chip?${readerQuery(readerOrIndex, maybeName)}`),
  esimChipCached: (readerOrIndex, maybeName) => j('GET', `/api/esim/chip/cached?${readerQuery(readerOrIndex, maybeName)}`),
  esimProfiles: (readerOrIndex, maybeName) => j('GET', `/api/esim/profiles?${readerQuery(readerOrIndex, maybeName)}`),
  esimEnable: (iccid, readerOrBody) => j(
    'POST',
    `/api/esim/profiles/${encodeURIComponent(iccid)}/enable`,
    readerBody(readerOrBody),
  ),
  esimDisable: (iccid, readerOrBody) => j(
    'POST',
    `/api/esim/profiles/${encodeURIComponent(iccid)}/disable`,
    readerBody(readerOrBody),
  ),
  esimDelete: (iccid, readerOrBody) => {
    if (readerOrBody && typeof readerOrBody === 'object') {
      const q = readerQuery(readerOrBody.reader ?? readerOrBody.reader_index)
      if (readerOrBody.se_id || readerOrBody.seId) q.set('se_id', readerOrBody.se_id || readerOrBody.seId)
      if (readerOrBody.aid) q.set('aid', readerOrBody.aid)
      return j('DELETE', `/api/esim/profiles/${encodeURIComponent(iccid)}?${q}`)
    }
    return j(
      'DELETE',
      `/api/esim/profiles/${encodeURIComponent(iccid)}?${readerQuery(readerOrBody)}`,
    )
  },
  esimNickname: (iccid, nickname, readerOrBody) => j(
    'POST',
    `/api/esim/profiles/${encodeURIComponent(iccid)}/nickname`,
    readerBody(readerOrBody, { nickname }),
  ),
  esimDownload: (body) => j('POST', '/api/esim/download', body),
  esimDownloadCancel: (readerOrBody) => j('POST', '/api/esim/download/cancel', readerBody(readerOrBody)),
  esimDiscovery: (body) => j('POST', '/api/esim/discovery', body || {}),
  esimNotifications: (readerOrIndex, maybeName) => j(
    'GET',
    `/api/esim/notifications?${readerQuery(readerOrIndex, maybeName)}`,
  ),
  // Aliases used by Esim.jsx
  esimProcessNotifications: (readerOrIndex, seq) => j(
    'POST',
    '/api/esim/notifications/process',
    readerBody(readerOrIndex, seq == null ? {} : { seq }),
  ),
  esimNotificationsProcess: (body) => j('POST', '/api/esim/notifications/process', body || {}),
  esimRemoveNotification: (seq, readerOrBody) => {
    if (readerOrBody && typeof readerOrBody === 'object') {
      const q = readerQuery(readerOrBody.reader ?? readerOrBody.reader_index)
      if (readerOrBody.se_id || readerOrBody.seId) q.set('se_id', readerOrBody.se_id || readerOrBody.seId)
      if (readerOrBody.aid) q.set('aid', readerOrBody.aid)
      return j('DELETE', `/api/esim/notifications/${seq}?${q}`)
    }
    return j(
      'DELETE',
      `/api/esim/notifications/${seq}?${readerQuery(readerOrBody)}`,
    )
  },
  esimNotificationRemove: (seq, readerOrBody) => {
    if (readerOrBody && typeof readerOrBody === 'object') {
      const q = readerQuery(readerOrBody.reader ?? readerOrBody.reader_index)
      if (readerOrBody.se_id || readerOrBody.seId) q.set('se_id', readerOrBody.se_id || readerOrBody.seId)
      if (readerOrBody.aid) q.set('aid', readerOrBody.aid)
      return j('DELETE', `/api/esim/notifications/${seq}?${q}`)
    }
    return j(
      'DELETE',
      `/api/esim/notifications/${seq}?${readerQuery(readerOrBody)}`,
    )
  },
}

export function connectWs(onMsg, onAuthLost, onOriginRefused) {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  let ws, alive = true
  const open = () => {
    // The marker lets the server distinguish clients that understand the 4401 close code
    // from an already-open pre-upgrade tab that would otherwise reconnect forever.
    ws = new WebSocket(`${proto}://${location.host}/ws?auth_close=1`)
    ws.onmessage = (e) => { try { onMsg(JSON.parse(e.data)) } catch {} }
    ws.onclose = (event) => {
      if (event.code === 4401) {
        alive = false
        onAuthLost?.()
        return
      }
      // 4403: the gateway does not recognise this page's origin -- a reverse proxy rewrote the
      // Host header without being listed as trusted. Retrying cannot fix that; say so instead.
      if (event.code === 4403) {
        alive = false
        onOriginRefused?.()
        return
      }
      if (alive) setTimeout(open, 2000)
    }
    ws.onerror = () => { try { ws.close() } catch {} }
  }
  open()
  return () => { alive = false; try { ws.close() } catch {} }
}
