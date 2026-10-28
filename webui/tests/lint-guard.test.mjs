// `npm run build` must refuse a JSX component or identifier that is neither defined nor
// imported. Vite only transpiles, so without the ESLint step such a reference ships and blanks
// the page at runtime (UnrecognizedUsbDevices on the devices page, 1.14.0-slim.5).
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import { ESLint } from 'eslint'

const root = new URL('..', import.meta.url).pathname

async function lint(code) {
  const eslint = new ESLint({ cwd: root })
  const [result] = await eslint.lintText(code, { filePath: `${root}src/lint-guard-probe.jsx` })
  return result.messages.map(message => `${message.ruleId}: ${message.message}`)
}

test('an undefined JSX component is an error', async () => {
  const messages = await lint("export function Page() { return <div><MissingPanel /></div> }\n")
  assert.deepEqual(messages, ["react/jsx-no-undef: 'MissingPanel' is not defined."])
})

test('an undefined plain identifier is an error', async () => {
  const messages = await lint('export const value = missingHelper(1)\n')
  assert.deepEqual(messages, ["no-undef: 'missingHelper' is not defined."])
})

test('defined and imported components pass', async () => {
  const messages = await lint(
    "import { Empty } from './views/UnifiedPages.jsx'\n" +
    'function Local() { return null }\n' +
    'export function Page() { return <div><Local /><Empty /></div> }\n')
  assert.deepEqual(messages, [])
})

test('the build script runs the linter before vite', () => {
  const { scripts } = JSON.parse(readFileSync(`${root}package.json`, 'utf8'))
  assert.match(scripts.build, /^eslint \. && vite build$/)
})
