import assert from 'node:assert/strict'
import { test } from 'node:test'
import { verificationCode } from '../src/smsText.js'

test('finds a standalone code', () => {
  assert.equal(verificationCode('Your code is 482913. Do not share it.'), '482913')
  assert.equal(verificationCode('G-123456 is your Google verification code'), '123456')
  assert.equal(verificationCode('Use 4821 to log in'), '4821')
  assert.equal(verificationCode('验证码：908172，5分钟内有效'), '908172')
})

test('reads a code written with a space or dash', () => {
  assert.equal(verificationCode('Your code: 123 456'), '123456')
  assert.equal(verificationCode('Code 123-456'), '123456')
})

test('does not take a phone number, a long number or a price for a code', () => {
  assert.equal(verificationCode('Call +447700900123 now'), '')
  assert.equal(verificationCode('Reference 123456789012'), '')
  assert.equal(verificationCode('Balance is 1,234.50'), '')
  assert.equal(verificationCode('no digits here'), '')
  assert.equal(verificationCode(''), '')
})
