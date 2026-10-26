import React, { useCallback, useEffect, useRef, useState } from 'react'
import { api, connectWs, setCsrf } from './api.js'
import Softphone from './views/Softphone.jsx'
import GlobalSoftphone from './GlobalSoftphone.jsx'
import Messages from './views/Messages.jsx'
import Esim from './views/Esim.jsx'
import Keepalive from './views/Keepalive.jsx'
import { UnifiedOverview, DevicesPage, EgressPage, NotificationsPage, SystemPage, DiagnosticsPage } from './views/UnifiedPages.jsx'
import ContactsPage from './views/Contacts.jsx'
import { useI18n } from './i18n.jsx'

const NAV = [
  ['overview', 'Overview', '⌂'], ['devices', 'Devices', '▣'], ['calls', 'Calls', '☎'],
  ['messages', 'Messages', '✉'], ['contacts', 'Contacts', '☏'],
  ['esim', 'eSIM', '◎'], ['keepalive', 'Balance & keeping', '◷'],
  ['egress', 'Network exits', '⇄'],
  ['notifications', 'Notifications', '◉'], ['settings', 'System settings', '⚙'], ['diagnostics', 'Diagnostics', '≣'],
]

// Each page is addressable as #/<key>, so a refresh (or a bookmark) lands on the same page
// instead of falling back to the overview. An unknown hash means the overview.
const viewFromHash = () => {
  const key = window.location.hash.replace(/^#\/?/, '')
  return NAV.some(([k]) => k === key) ? key : 'overview'
}

function lineCapabilityState(status, desired = true) {
  const state = String(status?.state || '').toUpperCase()
  if (state === 'OK') return 'on'
  if (state === 'STOPPED') return desired ? 'degraded' : 'off'
  if (['ERROR', 'NO_CARD', 'PIN_PROBLEM'].includes(state)) return 'error'
  return desired ? 'starting' : 'off'
}

function mergeLiveLineStatus(device, status) {
  const currentCapability = device.capabilities?.vowifi || {}
  const isDraft = device.provisioning?.state === 'draft'
  // A draft has two simultaneously true backend facts: its engine is stopped and automatic
  // setup is waiting for required SIM/hardware fields.  The periodic device snapshot exposes
  // the useful provisioning explanation, while a generic live STOPPED event only describes
  // the engine.  Preserve the draft explanation so those two feeds cannot make the card text
  // alternate every few seconds.
  const actual = isDraft
    ? (currentCapability.actual || 'off')
    : lineCapabilityState(status, currentCapability.desired !== false)
  const reason = isDraft
    ? (currentCapability.reason || 'Automatic setup is waiting for SIM or hardware information')
    : (status.reason || '')
  return {
    ...device,
    status,
    vowifi: {
      ...(device.vowifi || {}),
      epdg: status.detail || {},
      ims: isDraft ? (device.vowifi?.ims || '') : (status.label || ''),
    },
    capabilities: {
      ...(device.capabilities || {}),
      vowifi: { ...currentCapability, actual, reason },
    },
  }
}

function legacyDevices(instances, cards) {
  const used = new Set()
  const fromInstances = instances.map((inst, i) => {
    const reader = inst.reader || inst.reader_name || inst.config?.reader
    if (reader) used.add(reader)
    const state = String(inst.status?.state || '').toUpperCase()
    const running = ['OK', 'WORKING', 'REGISTERED'].includes(state) || inst.status?.label === 'Working'
    return {
      id: inst.device_id || inst.id,
      name: inst.name || inst.id,
      reader,
      model: inst.modem_name || inst.modem,
      sim: { name: inst.carrier || inst.name, number: inst.number || inst.msisdn },
      status: inst.status,
      compatibilityOnly: true,
      capabilities: {
        cellular: { desired: false, actual: 'unsupported', reason: 'Unified cellular status has not been exposed by this backend.' },
        vowifi: { desired: running, actual: running ? 'on' : (state === 'ERROR' ? 'error' : 'off'), reason: inst.status?.reason || '' },
      },
    }
  })
  const readers = cards.filter(c => !used.has(c.reader || c.name)).map((c, i) => ({
    id: `reader:${c.reader || c.name || i}`, name: c.modem_name || c.reader || c.name || `Reader ${i + 1}`,
    reader: c.reader || c.name, present: c.present !== false, sim: { name: c.carrier || 'SIM' }, compatibilityOnly: true,
    capabilities: { cellular: { desired: false, actual: 'unsupported' }, vowifi: { desired: false, actual: 'off' } },
  }))
  return [...fromInstances, ...readers]
}

export default function App() {
  const { t } = useI18n()
  const [view, setView] = useState(viewFromHash); const [menuOpen, setMenuOpen] = useState(false)
  const [instances, setInstances] = useState([]); const [cards, setCards] = useState([]); const [devices, setDevices] = useState([])
  // Sessions live in memory, so signing in normally happens seconds after the control plane
  // restarted — while its first card scan is still running. Until that scan has answered,
  // an empty list means "not known yet", not "no devices".
  const [discovering, setDiscovering] = useState(true)
  const [initialLoading, setInitialLoading] = useState(true)
  const [loadErrors, setLoadErrors] = useState({})
  const [selected, setSelected] = useState(null); const [toast, setToast] = useState(null)
  const [selectedDeviceId, setSelectedDeviceId] = useState(null)
  const [deviceTab, setDeviceTab] = useState(null)
  const [theme, setTheme] = useState(() => localStorage.getItem('theme') || 'auto')
  const [systemMeta, setSystemMeta] = useState({ version: '', repository_url: '' })
  // Unread messages across every line, for the badge on Messages. The page itself keeps the
  // per-conversation counts; this is refreshed when a message arrives and when one is read.
  const [unreadTotal, setUnreadTotal] = useState(0)
  const [unreadLines, setUnreadLines] = useState({})
  const unreadRequest = useRef(0)
  const loadUnread = useCallback(async () => {
    const request = ++unreadRequest.current
    try {
      const summary = await api.unreadTotal()
      const lines = await Promise.all((summary.lines || []).map(async id => {
        try { return [id, Number((await api.unreadMessages(id)).total) || 0] }
        catch { return [id, 0] }
      }))
      if (request === unreadRequest.current) {
        setUnreadTotal(Number(summary.total) || 0)
        setUnreadLines(Object.fromEntries(lines))
      }
    } catch {}
  }, [])
  const [authState, setAuthState] = useState(null)
  const wsEvents = useRef({ handlers: new Set() }); const toastTimer = useRef(null); const unifiedAvailable = useRef(false)
  const refreshInFlight = useRef(false)

  useEffect(() => { document.documentElement.dataset.theme = theme; localStorage.setItem('theme', theme) }, [theme])
  // Keep the address bar on the current page without growing history, and follow the hash
  // when the user edits it or navigates back/forward (replaceState never fires hashchange,
  // so the two effects cannot feed each other).
  useEffect(() => {
    const wanted = `#/${view}`
    if (window.location.hash !== wanted) window.history.replaceState(null, '', wanted)
  }, [view])
  useEffect(() => {
    const onHash = () => setView(viewFromHash())
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])
  const showToast = useCallback((message) => { clearTimeout(toastTimer.current); setToast({ message, id: Date.now() }); toastTimer.current=setTimeout(()=>setToast(null),5000) }, [])
  const expireAuth=useCallback(()=>{
    setCsrf('')
    setAuthState(s=>({...s,configured:true,authenticated:false,csrf:''}))
  },[])

  const refresh = useCallback(async () => {
    if (refreshInFlight.current) return
    refreshInFlight.current = true
    try {
      const [instancesResult, cardsResult, devicesResult] = await Promise.allSettled([
        api.instances(), api.cards(), api.devices(),
      ])
      setInitialLoading(false)
      setLoadErrors({
        instances: instancesResult.status === 'rejected',
        cards: cardsResult.status === 'rejected',
        devices: devicesResult.status === 'rejected' && devicesResult.reason?.status !== 404,
      })
      const nextInstances = instancesResult.status === 'fulfilled' ? instancesResult.value.instances || [] : null
      const nextCards = cardsResult.status === 'fulfilled' ? cardsResult.value.cards || [] : null
      if (nextInstances) {
        setInstances(nextInstances)
        // Selection is view context, not a global default. In particular, opening an offline
        // device must never silently put the first unrelated saved SIM into its edit/delete
        // form. Calls and Messages select their first live line in SimSelector instead.
        setSelected(s => s && nextInstances.some(item => String(item.id) === String(s)) ? s : null)
      }
      if (nextCards) setCards(nextCards)
      if (devicesResult.status === 'fulfilled') {
        const r=devicesResult.value; const list=Array.isArray(r)?r:(r.devices||[])
        unifiedAvailable.current=true; setDevices(list); setDiscovering(!!r.discovering)
      // Compatibility mode is only for an older backend that does not implement the unified
      // endpoint. A transient network failure must not turn every saved line and reader into
      // a temporary "device" until the next poll succeeds.
      } else if (devicesResult.reason?.status === 404 && nextInstances && nextCards) {
        unifiedAvailable.current=false; setDevices(legacyDevices(nextInstances,nextCards)); setDiscovering(false)
      }
    } finally {
      refreshInFlight.current = false
    }
  }, [])
  useEffect(()=>{
    window.addEventListener('mdd-auth-expired',expireAuth)
    return()=>window.removeEventListener('mdd-auth-expired',expireAuth)
  },[expireAuth])
  useEffect(()=>{ api.authStatus().then(s=>{ setCsrf(s.csrf); setAuthState(s) }).catch(()=>setAuthState({configured:true,authenticated:false})) },[])
  useEffect(()=>{ if(authState?.authenticated) refresh() },[authState?.authenticated]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(()=>{ if(!authState?.authenticated)return;
    // Status refreshes must retain release/repository metadata loaded by separate requests.
    const load=()=>api.systemStatus().then(status=>setSystemMeta(s=>({...s,...status}))).catch(()=>{})
    load(); const timer=setInterval(load,60*1000); return()=>clearInterval(timer) },[authState?.authenticated])
  useEffect(()=>{ if(!authState?.authenticated)return; const timer=setInterval(refresh,10000); return()=>clearInterval(timer) },[refresh,authState?.authenticated])
  // The event socket was refused for the page's origin: live updates and the softphone are off
  // until the reverse proxy is fixed, so this stays up rather than being a passing toast.
  const [originRefused, setOriginRefused] = useState(false)
  useEffect(()=>{ if(authState?.authenticated) loadUnread() },[authState?.authenticated,loadUnread])

  useEffect(()=>{ if(!authState?.authenticated)return; return connectWs(msg=>{
    if(msg.type==='status'){
      const status=Object.fromEntries(Object.entries(msg).filter(([k])=>!['type','instance'].includes(k)))
      setInstances(list=>list.map(i=>String(i.id)===String(msg.instance)?{...i,status}:i))
      setDevices(list=>list.map(d=>String(d.instance_id)===String(msg.instance)
        ? mergeLiveLineStatus(d, status) : d))
    }
    // The card scan is what makes readers (and their lines) appear. Rebuild the device list
    // from it immediately instead of leaving the page empty until the next 10s poll.
    if(msg.type==='cards'){setCards(msg.cards||[]);refresh()}
    if(msg.type==='engine'&&['card_removed','reader_lost','reader_added','reader_removed'].includes(msg.event)){
      const name=msg.args?.[0]
      showToast({card_removed:t('SIM removed — line stopped'),reader_lost:t('Reader unplugged — line stopped'),reader_added:`${t('Card reader connected')}${name?`: ${name}`:''}`,reader_removed:`${t('Card reader disconnected')}${name?`: ${name}`:''}`}[msg.event])
    }
    if(['device','capability','cellular','engine'].includes(msg.type)) refresh()
    if(msg.type==='sms') loadUnread()
    wsEvents.current.handlers.forEach(h=>h(msg))
    if(msg.type==='sms'&&msg.message?.direction==='in')showToast(t('SMS from {peer}',{peer:msg.message.peer}))
    if(msg.type==='call'&&msg.call?.direction==='in')showToast(t('Incoming call from {peer}',{peer:msg.call.peer}))
  },expireAuth,()=>setOriginRefused(true))},[refresh,showToast,t,authState?.authenticated,expireAuth,loadUnread])
  const subscribe=useCallback(h=>{wsEvents.current.handlers.add(h);return()=>wsEvents.current.handlers.delete(h)},[])
  if (!authState) return <div className="auth-shell"><div className="auth-card"><h1>MDD Sim Gateway</h1><p>{t('Loading…')}</p></div></div>
  if (!authState.authenticated) return <AuthScreen configured={authState.configured} accountUsername={authState.username} t={t} onDone={result=>{setCsrf(result.csrf);setAuthState(s=>({...s,configured:true,authenticated:true,csrf:result.csrf}))}} />
  const sel=instances.find(i=>i.id===selected)
  const common={devices,discovering,initialLoading,loadErrors,refreshDevices:refresh,instances,cards,selected:sel,setSelected,refresh,subscribe,showToast,setView,selectedDeviceId,setSelectedDeviceId,deviceTab,setDeviceTab,setSystemMeta,refreshUnread:loadUnread,unreadLines}
  const content={
    overview:<UnifiedOverview {...common}/>, devices:<DevicesPage {...common}/>, calls:<Softphone {...common}/>,
    messages:<Messages {...common}/>, esim:<Esim {...common}/>, keepalive:<Keepalive {...common}/>,
    egress:<EgressPage {...common}/>,
    notifications:<NotificationsPage {...common}/>,
    contacts:<ContactsPage showToast={showToast}/>,
    settings:<SystemPage {...common}/>, diagnostics:<DiagnosticsPage {...common}/>,
  }[view]
  const issueUrl = `${(systemMeta.repository_url || 'https://github.com/MddIdd/mdd-sim-gateway').replace(/\/$/, '')}/issues/new/choose`
  return <div className="u-shell">
    <GlobalSoftphone instances={instances} excludedId={view === 'calls' ? sel?.id : null} showToast={showToast} />
    <aside className={`u-sidebar ${menuOpen?'open':''}`}>
      <div className="u-brand"><img src="/logo.svg" alt="" /><div>MDD Sim Gateway<small>{t('4G + VoWiFi unified')}</small></div></div>
      <nav>{NAV.map(([key,label,icon])=><button key={key} className={view===key?'active':''} onClick={()=>{setView(key);setMenuOpen(false)}}><span>{icon}</span>{t(label)}{key==='diagnostics'&&!!systemMeta.host_alerts?.length&&<i className={`u-nav-dot ${systemMeta.host_alerts.some(a=>a.severity==='critical')?'critical':'warning'}`} title={t('The gateway host needs attention')}/>}{key==='calls'&&!!systemMeta.unheard_voicemails&&<i className="u-nav-dot critical" title={t('There are voicemails you have not played')}/>}{key==='messages'&&unreadTotal>0&&<span className="u-nav-count" aria-label={t('{count} unread',{count:unreadTotal})}>{unreadTotal>99?'99+':unreadTotal}</span>}</button>)}</nav>
      <div className="u-sidebar-foot"><div className="u-theme">{[['auto','◐'],['light','☀'],['dark','☾']].map(([k,x])=><button key={k} className={theme===k?'active':''} onClick={()=>setTheme(k)} title={t(k)}>{x}</button>)}</div><small>{discovering&&!devices.length?t('Detecting devices…'):`${devices.length} ${t(devices.length === 1 ? 'device' : 'devices')}`}</small><a className="u-feedback-link" href={issueUrl} target="_blank" rel="noreferrer"><span>◉</span>{t('Issues and suggestions')}<b>↗</b></a><div className="u-project-meta"><span className="u-version">{systemMeta.version ? `v${systemMeta.version}` : '—'}</span>{systemMeta.repository_url&&<a className="u-repo-actions" href={systemMeta.repository_url} target="_blank" rel="noreferrer">{t('Source code')}</a>}</div><button className="btn btn-ghost" onClick={async()=>{try{await api.authLogout()}finally{setCsrf('');setAuthState(s=>({...s,configured:true,authenticated:false,csrf:''}))}}}>{t('Sign out')}</button></div>
    </aside>
    <button className="u-menu" onClick={()=>setMenuOpen(!menuOpen)}>☰</button>
    {menuOpen&&<button className="u-scrim" aria-label={t('Close menu')} onClick={()=>setMenuOpen(false)}/>}
    <main className="u-main"><header><div><h1>{t(NAV.find(x=>x[0]===view)?.[1]||view)}</h1><p>{t(`page.${view}.subtitle`)}</p></div><div className="u-live"><span className="u-dot" />{initialLoading?t('Loading…'):loadErrors.devices?t('Loading failed'):unifiedAvailable.current?t('Live device control'):t('Compatibility view')}</div></header><div className="u-content">{originRefused&&<div className="u-update-banner rollback" role="alert"><b>{t('Live updates and the softphone are off: the gateway does not recognise the address this page was opened from.')}</b><span>{t('A reverse proxy in front of the gateway must keep the Host header (nginx: proxy_set_header Host $http_host;), or be listed under Settings → Security → Trusted reverse proxies and send X-Forwarded-Host.')}</span></div>}<div className="u-note" role="note">{t('Responsible use notice')}</div>{content}</div></main>
    {toast&&<div className="u-toast" key={toast.id} role="status">{toast.message}</div>}
  </div>
}

function AuthScreen({ configured, accountUsername, t, onDone }) {
  const [username,setUsername]=useState(configured ? (accountUsername || 'admin') : 'admin'); const [password,setPassword]=useState(''); const [confirm,setConfirm]=useState(''); const [error,setError]=useState(''); const [busy,setBusy]=useState(false); const [retry,setRetry]=useState(0); const [remember,setRemember]=useState(true)
  useEffect(()=>{if(!retry)return;const timer=setInterval(()=>setRetry(v=>Math.max(0,v-1)),1000);return()=>clearInterval(timer)},[retry])
  const submit=async()=>{if(busy||retry||!password)return;setError('');if(!configured&&password!==confirm){setError(t('Passwords do not match'));return}setBusy(true);try{onDone(await (configured?api.authLogin(username,password,remember):api.authSetup(username,password,remember)))}catch(err){if(err.status===429){const seconds=Math.max(1,Number(err.data?.retry_after)||60);setRetry(seconds);setError(t('Too many attempts. Try again in {seconds} seconds.',{seconds}))}else setError(err.message)}finally{setBusy(false)}}
  return <div className="auth-shell"><form className="auth-card" onSubmit={e=>{e.preventDefault();submit()}}><div className="auth-brand"><div className="auth-mark">M</div><h1>MDD Sim Gateway</h1></div><p>{t(configured?'Sign in to manage the gateway':'Create the administrator account')}</p><label>{t('Username')}<input value={username} onChange={e=>setUsername(e.target.value)} readOnly={configured} autoComplete="off" data-1p-ignore="true" data-lpignore="true" required /></label><label>{t('Password')}<input type="password" value={password} onChange={e=>setPassword(e.target.value)} autoComplete="off" data-1p-ignore="true" data-lpignore="true" minLength="10" required /></label>{!configured&&<label>{t('Confirm password')}<input type="password" value={confirm} onChange={e=>setConfirm(e.target.value)} autoComplete="new-password" minLength="10" required /></label>}<label className="auth-remember"><input type="checkbox" checked={remember} onChange={e=>setRemember(e.target.checked)} />{t('Keep me signed in for 30 days')}</label>{error&&<p className="auth-error">{retry?t('Too many attempts. Try again in {seconds} seconds.',{seconds:retry}):error}</p>}<button type="submit" className="primary" disabled={busy||retry>0||!password}>{retry?t('Try again in {seconds}s',{seconds:retry}):t(busy?'Please wait…':configured?'Sign in':'Create account')}</button>{!configured&&<small>{t('Use at least 10 characters. Reset it from the host if it is lost.')}</small>}</form></div>
}
