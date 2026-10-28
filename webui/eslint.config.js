// Catches what `vite build` lets through: a name used but never defined or imported — a plain
// identifier (no-undef) or a JSX component (react/jsx-no-undef). Vite only transpiles, so such
// a reference otherwise reaches the device and blanks the page at runtime. `npm run build`
// runs this first. Style rules are deliberately left out.
import react from 'eslint-plugin-react'
import reactHooks from 'eslint-plugin-react-hooks'
import globals from 'globals'

export default [
  { ignores: ['dist/**', 'node_modules/**'] },
  { linterOptions: { reportUnusedDisableDirectives: 'off' } },
  {
    files: ['src/**/*.{js,jsx}', 'tests/**/*.{js,mjs}'],
    // react-hooks is registered only so the existing `eslint-disable-line react-hooks/...`
    // comments resolve; none of its rules is turned on.
    plugins: { react, 'react-hooks': reactHooks },
    languageOptions: {
      ecmaVersion: 'latest',
      sourceType: 'module',
      parserOptions: { ecmaFeatures: { jsx: true } },
      globals: { ...globals.browser, __APP_VERSION__: 'readonly' },
    },
    rules: {
      'no-undef': 'error',
      'react/jsx-no-undef': ['error', { allowGlobals: false }],
      'react/jsx-uses-vars': 'error',
    },
  },
  { files: ['tests/**/*.{js,mjs}'], languageOptions: { globals: globals.node } },
]
