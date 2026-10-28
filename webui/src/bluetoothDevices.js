// Device list order for the Bluetooth page (views/Bluetooth.jsx); kept in plain JS so the node
// tests can import it.

// Paired first, then audio devices, then the rest; by name within each group.
export function sortDevices(devices) {
  const rank = d => (d.paired ? 0 : d.audio ? 1 : 2)
  return [...devices].sort((a, b) => rank(a) - rank(b) || a.name.localeCompare(b.name))
}

// bluez names an unnamed (usually BLE) device after its address; those stay hidden unless
// asked for, except a paired one.
export function visibleDevices(devices, showAll) {
  return sortDevices(devices).filter(d => showAll || d.named || d.paired)
}
