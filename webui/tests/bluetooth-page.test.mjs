// The device list: paired first, then audio devices; unnamed BLE beacons hidden by default.
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { sortDevices, visibleDevices } from '../src/bluetoothDevices.js'

const devices = [
  { mac: '98:FC:84:01:5C:1B', name: '98-FC-84-01-5C-1B', named: false, paired: false, audio: false },
  { mac: 'AA:00:00:00:00:02', name: 'Kitchen speaker', named: true, paired: false, audio: true },
  { mac: 'AA:00:00:00:00:03', name: 'Band', named: true, paired: false, audio: false },
  { mac: '98:FC:84:8A:B9:AD', name: 'MI Portable Speaker', named: true, paired: true, audio: true },
]

test('paired, then audio, then the rest', () => {
  assert.deepEqual(sortDevices(devices).map(d => d.name),
    ['MI Portable Speaker', 'Kitchen speaker', '98-FC-84-01-5C-1B', 'Band'])
})

test('unnamed devices are hidden unless asked for', () => {
  assert.deepEqual(visibleDevices(devices, false).map(d => d.mac),
    ['98:FC:84:8A:B9:AD', 'AA:00:00:00:00:02', 'AA:00:00:00:00:03'])
  assert.equal(visibleDevices(devices, true).length, 4)
})
