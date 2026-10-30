// Helpers for the received-texts page (views/Sms.jsx); plain JS so the node tests can import it.

// A verification code: 4-8 digits standing alone (not part of a longer number or a phone
// number), optionally written as "123-456" or "123 456". Returns the digits, or ''.
export function verificationCode(body) {
  const text = String(body || '')
  const spaced = text.match(/(?<![\d+])(\d{3})[ -](\d{3})(?![\d])/)
  if (spaced) return spaced[1] + spaced[2]
  const plain = text.match(/(?<![\d+.,])(?<!\d-)(\d{4,8})(?!\d)(?![.,]\d)/)
  return plain ? plain[1] : ''
}

export function groupByLine(messages) {
  const groups = new Map()
  for (const message of messages) {
    const key = String(message.instance)
    if (!groups.has(key)) groups.set(key, [])
    groups.get(key).push(message)
  }
  return groups
}
