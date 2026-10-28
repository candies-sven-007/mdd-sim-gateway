import React, { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { useI18n } from '../i18n.jsx'
import { visibleDevices } from '../bluetoothDevices.js'

// Bluetooth speaker page: scan, pair, connect, volume and the boot-time auto-connect address.
// Every action is one call of the root helper behind /api/bluetooth (control/app/routers/
// bluetooth.py); this page only shows its state and polls it.
const SCAN_SECONDS = 30
const POLL_SCANNING_MS = 1000
const POLL_IDLE_MS = 3000
const VOLUME_SETTLE_MS = 200

function DeviceRow({ device, autoconnect, busy, onAction, onAutoconnect, t }) {
  const [confirmRemove, setConfirmRemove] = useState(false)
  const [confirmDisconnect, setConfirmDisconnect] = useState(false)
  const isAuto = autoconnect === device.mac
  const disabled = !!busy
  const state = device.connected ? t('Connected') : device.paired ? t('Paired') : t('Not paired')
  return <div className="u-bt-row">
    <div>
      <h4>{device.name || device.mac}{device.audio && <span className="u-badge"> ♪</span>}</h4>
      <p className="mono">{device.mac} · {state}</p>
      {device.paired && <label className="u-inline-check">
        <input type="checkbox" checked={isAuto} disabled={disabled}
          onChange={e => onAutoconnect(e.target.checked ? device.mac : null)} />
        {t('Connect automatically at boot')}
      </label>}
    </div>
    <div className="u-proxy-actions">
      {busy === device.mac && <span className="u-note">{t('Working…')}</span>}
      {!device.paired && <button className="btn btn-primary" disabled={disabled}
        onClick={() => onAction(device.mac, 'pair')}>{t('Pair')}</button>}
      {device.paired && !device.connected && <button className="btn btn-primary" disabled={disabled}
        onClick={() => onAction(device.mac, 'connect')}>{t('Connect')}</button>}
      {device.connected && !confirmDisconnect && <button className="btn btn-ghost" disabled={disabled}
        onClick={() => isAuto ? setConfirmDisconnect(true) : onAction(device.mac, 'disconnect')}>{t('Disconnect')}</button>}
      {device.connected && confirmDisconnect && <>
        <span className="u-note">{t('This speaker is set to connect automatically, so it will reconnect within about 30 seconds.')}</span>
        <button className="btn btn-ghost" disabled={disabled}
          onClick={() => { setConfirmDisconnect(false); onAction(device.mac, 'disconnect') }}>{t('Disconnect for now')}</button>
        <button className="btn btn-primary" disabled={disabled}
          onClick={async () => { setConfirmDisconnect(false); await onAutoconnect(null); await onAction(device.mac, 'disconnect') }}>{t('Disconnect and stop auto-connect')}</button>
        <button className="btn btn-ghost" onClick={() => setConfirmDisconnect(false)}>{t('Cancel')}</button>
      </>}
      {device.paired && !confirmRemove && <button className="btn btn-ghost u-proxy-remove" disabled={disabled}
        onClick={() => setConfirmRemove(true)}>{t('Remove pairing')}</button>}
      {device.paired && confirmRemove && <>
        <span className="u-note">{t('Remove the pairing with {name}?', { name: device.name || device.mac })}</span>
        <button className="btn btn-primary" disabled={disabled}
          onClick={() => { setConfirmRemove(false); onAction(device.mac, 'remove') }}>{t('Remove')}</button>
        <button className="btn btn-ghost" onClick={() => setConfirmRemove(false)}>{t('Cancel')}</button>
      </>}
    </div>
  </div>
}

export default function BluetoothPage({ showToast }) {
  const { t } = useI18n()
  const [status, setStatus] = useState(null)
  const [loadError, setLoadError] = useState('')
  const [showAll, setShowAll] = useState(false)
  const [busy, setBusy] = useState('')            // MAC (or 'scan') of the action in progress
  const [actionError, setActionError] = useState(null)
  const [scanEndsAt, setScanEndsAt] = useState(0)
  const [now, setNow] = useState(Date.now())
  const [volume, setVolume] = useState(null)      // local slider value while dragging
  const volumeTimer = useRef(null)

  const load = useCallback(async () => {
    try {
      const next = await api.bluetoothStatus()
      setStatus(next)
      setLoadError('')
    } catch (error) { setLoadError(error.message) }
  }, [])

  const scanning = !!status?.scanning || scanEndsAt > now
  useEffect(() => {
    load()
    const timer = setInterval(() => {
      setNow(Date.now())
      if (document.visibilityState === 'visible') load()
    }, scanning ? POLL_SCANNING_MS : POLL_IDLE_MS)
    return () => clearInterval(timer)
  }, [load, scanning])
  useEffect(() => {
    if (!scanEndsAt) return
    const timer = setInterval(() => setNow(Date.now()), 250)
    return () => clearInterval(timer)
  }, [scanEndsAt])
  useEffect(() => () => clearTimeout(volumeTimer.current), [])

  const run = async (key, work, failureHint) => {
    setBusy(key); setActionError(null)
    try { await work(); await load() }
    catch (error) {
      setActionError({ message: error.message, hint: failureHint })
      await load()
    } finally { setBusy('') }
  }

  const scan = () => run('scan', async () => {
    await api.bluetoothScan()
    setScanEndsAt(Date.now() + SCAN_SECONDS * 1000)
  })
  const deviceAction = (mac, action) => run(mac, async () => {
    await api.bluetoothDevice(mac, action)
    showToast?.(t({ pair: 'Paired and connected', connect: 'Connected', disconnect: 'Disconnected', remove: 'Pairing removed' }[action]))
  }, action === 'pair' || action === 'connect'
    ? 'Put the speaker in pairing mode and disconnect it from your phone, then try again.' : '')
  const setAutoconnect = mac => run(mac || 'auto', () => api.bluetoothAutoconnect(mac))

  const sendVolume = value => {
    clearTimeout(volumeTimer.current)
    volumeTimer.current = setTimeout(async () => {
      try { await api.bluetoothVolume({ volume: value }); await load() }
      catch (error) { showToast?.(`${t('Error')}: ${error.message}`) }
      finally { setVolume(null) }
    }, VOLUME_SETTLE_MS)
  }
  const toggleMute = async () => {
    try { await api.bluetoothVolume({ muted: !status.sink.muted }); await load() }
    catch (error) { showToast?.(`${t('Error')}: ${error.message}`) }
  }

  if (!status) return <div className="card u-panel"><p className="u-note">{loadError ? `${t('Error')}: ${loadError}` : t('Loading…')}</p></div>
  if (!status.available) return <div className="card u-panel">
    <h3>{t('Bluetooth is not available on this gateway')}</h3>
    <p className="u-note">{t(status.reason || 'unavailable')}</p>
  </div>

  const sink = status.sink
  const devices = visibleDevices(status.devices || [], showAll)
  const hidden = (status.devices || []).length - devices.length
  const speakerName = sink?.description || (status.devices || []).find(d => d.connected && d.audio)?.name
  const secondsLeft = Math.max(0, Math.ceil((scanEndsAt - now) / 1000))
  const shownVolume = volume ?? sink?.volume ?? 0

  return <>
    <div className="card u-panel">
      <h3>{speakerName ? t('Speaker: {name}', { name: speakerName }) : t('No speaker connected')}</h3>
      {!status.powered && <p className="u-note">{t('The Bluetooth controller is off; scanning turns it on.')}</p>}
      {status.audio_reason && <p className="u-error">{t(status.audio_reason)}</p>}
      <div className="u-form-grid">
        <div className="u-bt-volume">
          <label>{t('Volume')}: {sink ? `${shownVolume}%` : '—'}</label>
          <input type="range" min="0" max="100" step="1" disabled={!sink} value={shownVolume}
            onChange={e => { const value = +e.target.value; setVolume(value); sendVolume(value) }} />
        </div>
        <div>
          <label>{t('Mute')}</label>
          <button className={`btn ${sink?.muted ? 'btn-primary' : 'btn-ghost'}`} disabled={!sink} onClick={toggleMute}>
            {sink?.muted ? t('Muted — tap to unmute') : t('Mute')}</button>
        </div>
      </div>
    </div>

    <div className="card u-panel">
      <div className="u-proxy-actions">
        <button className="btn btn-primary" disabled={scanning || busy === 'scan'} onClick={scan}>
          {scanning ? t('Scanning… {seconds} s', { seconds: secondsLeft || SCAN_SECONDS }) : t('Scan for devices')}</button>
        <label className="u-inline-check"><input type="checkbox" checked={showAll} onChange={e => setShowAll(e.target.checked)} />
          {t('Show all devices')}{hidden > 0 && !showAll ? ` (${t('{count} hidden', { count: hidden })})` : ''}</label>
      </div>
      {busy && busy !== 'scan' && busy !== 'auto' && (status.devices || []).some(d => d.mac === busy && !d.paired) &&
        <p className="u-note">{t('Pairing can take up to 40 seconds. Keep the speaker in pairing mode.')}</p>}
      {actionError && <div className="u-error">
        <p>{actionError.message}</p>
        {actionError.hint && <p>{t(actionError.hint)}</p>}
      </div>}
      {!devices.length && <p className="u-note">{t('No devices yet. Put the speaker in pairing mode and scan.')}</p>}
      {devices.map(device => <DeviceRow key={device.mac} device={device} autoconnect={status.autoconnect}
        busy={busy} onAction={deviceAction} onAutoconnect={setAutoconnect} t={t} />)}
    </div>
  </>
}
