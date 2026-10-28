// A line the relay refuses (not running, softphone off, ...) must not be retried every few
// seconds for ever. JsSIP resets its own back-off whenever a socket opens, and the relay now
// accepts before closing with the reason, so the Softphone class stops JsSIP and waits itself.
import assert from 'node:assert/strict'
import { mock, test } from 'node:test'
import { Softphone, SOFTPHONE_REFUSALS, refusalRetryDelay, REFUSAL_RETRY_MAX_MS } from '../src/softphone.js'

test('the back-off doubles from 30 s and stops at 10 min', () => {
  assert.deepEqual([0, 1, 2, 3, 4, 5, 9].map(refusalRetryDelay),
    [30_000, 60_000, 120_000, 240_000, 480_000, 600_000, 600_000])
  assert.equal(REFUSAL_RETRY_MAX_MS, 600_000)
})

test('a refusal stops the UA, reports why, and starts again after the back-off', () => {
  mock.timers.enable({ apis: ['setTimeout'] })
  try {
    const events = []
    const phone = new Softphone((type, data) => events.push([type, data]))
    let stopped = 0
    phone.ua = { stop() { stopped += 1 } }
    phone.prov = { ws_path: '/api/instances/1/softphone/ws' }
    phone._host = 'gw'
    const restarts = []
    phone.start = (prov, host) => { restarts.push([prov, host]); phone.ua = { stop() {} }; return true }

    phone._holdOff(4409, 'this line is not running')
    assert.equal(stopped, 1)
    assert.equal(phone.ua, null)
    assert.deepEqual(events, [['refused', { code: 4409, reason: 'this line is not running',
      message: SOFTPHONE_REFUSALS[4409], retryInMs: 30_000 }]])

    mock.timers.tick(29_999)
    assert.equal(restarts.length, 0)
    mock.timers.tick(1)
    assert.deepEqual(restarts, [[phone.prov, 'gw']])

    phone._holdOff(4409, '')                           // refused again: wait twice as long
    assert.equal(events.at(-1)[1].retryInMs, 60_000)
  } finally {
    mock.timers.reset()
  }
})

test('stop() cancels a pending retry', () => {
  mock.timers.enable({ apis: ['setTimeout'] })
  try {
    const phone = new Softphone(() => {})
    phone.prov = {}
    let restarted = false
    phone.start = () => { restarted = true }
    phone.ua = { stop() {} }
    phone._holdOff(4502, '')
    phone.stop()
    mock.timers.tick(REFUSAL_RETRY_MAX_MS)
    assert.equal(restarted, false)
  } finally {
    mock.timers.reset()
  }
})
