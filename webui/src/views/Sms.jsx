import React, { useCallback, useEffect, useState } from 'react'
import { api } from '../api.js'
import { useI18n } from '../i18n.jsx'
import { verificationCode } from '../smsText.js'

// Received texts (control/app/routers/sms.py). Receive only: there is no way to send from here.
const POLL_MS = 15000

function formatTime(seconds) {
  if (!seconds) return ''
  const date = new Date(seconds * 1000)
  return date.toLocaleString(undefined, { hour12: false })
}

export default function SmsPage({ instances = [], subscribe, showToast, onUnread }) {
  const { t } = useI18n()
  const [messages, setMessages] = useState(null)
  const [retention, setRetention] = useState(30)
  const [line, setLine] = useState('')
  const [error, setError] = useState('')
  const [confirmClear, setConfirmClear] = useState(false)

  const load = useCallback(async () => {
    try {
      const result = await api.smsList(line)
      setMessages(result.messages || [])
      setRetention(result.retention_days || 30)
      onUnread?.(Object.values(result.unread || {}).reduce((sum, n) => sum + n, 0))
      setError('')
    } catch (e) { setError(e.message) }
  }, [line, onUnread])

  useEffect(() => {
    load()
    const timer = setInterval(() => { if (document.visibilityState === 'visible') load() }, POLL_MS)
    const unsubscribe = subscribe?.(msg => { if (msg.type === 'sms') load() })
    return () => { clearInterval(timer); unsubscribe?.() }
  }, [load, subscribe])

  // Reading the page marks what it shows as read, a moment after it appears.
  useEffect(() => {
    const unread = (messages || []).filter(m => !m.read).map(m => m.id)
    if (!unread.length) return
    const timer = setTimeout(async () => {
      try { await api.smsRead({ ids: unread }); load() } catch { /* shown on the next poll */ }
    }, 1500)
    return () => clearTimeout(timer)
  }, [messages, load])

  const copy = async code => {
    try { await navigator.clipboard.writeText(code); showToast?.(t('Code copied')) }
    catch { showToast?.(t('Copy failed; select the code and copy it by hand')) }
  }
  const remove = async id => {
    try { await api.smsDelete(id); load() } catch (e) { showToast?.(`${t('Error')}: ${e.message}`) }
  }
  const clear = async () => {
    setConfirmClear(false)
    try { await api.smsClear(line); load() } catch (e) { showToast?.(`${t('Error')}: ${e.message}`) }
  }
  const lineName = id => (instances.find(i => String(i.id) === String(id)) || {}).name || `#${id}`

  if (messages === null) return <div className="card u-panel"><p className="u-note">{error ? `${t('Error')}: ${error}` : t('Loading…')}</p></div>

  return <div className="card u-panel">
    <div className="u-proxy-actions" style={{ justifyContent: 'space-between' }}>
      <div>
        <select value={line} onChange={e => setLine(e.target.value)}>
          <option value="">{t('All lines')}</option>
          {instances.map(i => <option key={i.id} value={i.id}>{i.name || `#${i.id}`}</option>)}
        </select>
      </div>
      {!!messages.length && !confirmClear && <button className="btn btn-ghost u-proxy-remove" onClick={() => setConfirmClear(true)}>{t('Delete all')}</button>}
      {confirmClear && <>
        <span className="u-note">{t('Delete all received messages shown here?')}</span>
        <button className="btn btn-primary" onClick={clear}>{t('Delete')}</button>
        <button className="btn btn-ghost" onClick={() => setConfirmClear(false)}>{t('Cancel')}</button>
      </>}
    </div>
    <p className="u-note">{t('Receive only. Messages are kept for {days} days and then deleted; they are stored on this device and included in its backups.', { days: retention })}</p>
    {error && <p className="u-error">{error}</p>}
    {!messages.length && <p className="u-note">{t('No messages yet.')}</p>}
    {messages.map(m => {
      const code = verificationCode(m.body)
      return <div key={m.id} className="u-sms-row">
        <div className="u-sms-head">
          <b>{m.sender || t('Unknown')}</b>
          {!m.read && <span className="u-badge"> {t('New')}</span>}
          <span className="u-note"> · {lineName(m.instance)} · {formatTime(m.sent_ts || m.received_ts)}</span>
        </div>
        <div className="u-sms-body">{m.body}</div>
        {!m.complete && <p className="u-note">{t('Some parts of this long message never arrived.')}</p>}
        <div className="u-proxy-actions" style={{ justifyContent: 'flex-start' }}>
          {code && <button className="btn btn-primary" onClick={() => copy(code)}>{t('Copy code {code}', { code })}</button>}
          <button className="btn btn-ghost u-proxy-remove" onClick={() => remove(m.id)}>{t('Delete')}</button>
        </div>
      </div>
    })}
  </div>
}
