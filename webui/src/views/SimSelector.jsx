import React, { useEffect } from 'react'
import { useI18n } from '../i18n.jsx'

// Per-page SIM/line picker for multi-SIM setups. Labels each line with the physical reader
// it currently occupies (from the detected-cards state) so it's clear which reader's engine
// will handle calls and logs. Switches the global `selected` instance.
//
// Only lines whose physical reader is currently PRESENT are listed — a provisioned line
// whose reader/card is unplugged is dropped from the list (its config stays under SIM
// Config and it reappears when the reader returns).
export default function SimSelector({ instances = [], cards = [], devices = [], selected, setSelected, label = 'Active SIM / line' }) {
  const { t, language } = useI18n()
  // A line is live when the card monitor or the device list shows its SIM present.
  const readerFor = (i) => cards.find((c) => c.present &&
    (String(c.matched) === String(i.id) || (c.iccid && c.iccid === i.iccid)))
  const deviceFor = (i) => devices.find((d) => d.present &&
    String(d.instance_id || '') === String(i.id))
  const sourceFor = (i) => readerFor(i) || deviceFor(i)
  const live = instances.filter((i) => sourceFor(i))
  const deviceName = (c) => {
    if (!c) return t('Unknown device')
    if (/SCR Prime/i.test(c.name || '')) return language === 'zh' ? '三体电子 SCR Prime 读卡器' : '3T Electronics SCR Prime reader'
    return c.display_name || c.name || t('Unknown device')
  }
  const lineName = (i) => i.carrier || i.name || [i.mcc, i.mnc].filter(Boolean).join('-') || t('Unknown SIM')
  // The line's VoWiFi state, as the device page shows it.
  const statusParts = (i) => {
    const device = devices.find((d) => String(d.instance_id || '') === String(i.id))
    const caps = device?.capabilities || {}
    const parts = []
    const vowifi = caps.vowifi || {}
    if (vowifi.actual === 'off' && vowifi.support?.status === 'unsupported') parts.push(`VoWiFi ${t('cap.unsupported')}`)
    else if (i.status?.label) parts.push(`VoWiFi ${t(i.status.label)}`)
    else if (vowifi.actual) parts.push(`VoWiFi ${t(`cap.${vowifi.actual}`)}`)
    return parts
  }

  // Calls/Messages own their useful default: choose the first live line here instead of in
  // App, where a global default could leak an unrelated line into a device's SIM tab.
  const id = selected?.id
  useEffect(() => {
    if (!id || !live.some((i) => String(i.id) === String(id))) setSelected(live[0]?.id || null)
  }, [id, live.map((i) => i.id).join(',')])  // eslint-disable-line react-hooks/exhaustive-deps

  if (!live.length) return null
  return (
    <div className="sim-picker card">
      <span className="sim-picker-label">{t(label)}</span>
      <div className="sim-picker-grid" role="group" aria-label={t(label)}>
        {live.map((i) => {
          const c = sourceFor(i)
          const model = deviceName(c)
          const name = lineName(i)
          const states = statusParts(i)
          const active = String(i.id) === String(id)
          return <button key={i.id} type="button" className={`sim-picker-item${active ? ' active' : ''}`}
            aria-pressed={active} onClick={() => setSelected(i.id)}>
            <span className="sim-picker-top"><strong>{name}</strong></span>
            <span className="sim-picker-device">{model}</span>
            <span className="sim-picker-number">{i.msisdn || '—'}</span>
            {!!states.length && <span className="sim-picker-status">{states.map((state, index) =>
              <span key={index}>{state}</span>)}</span>}
          </button>
        })}
      </div>
    </div>
  )
}
