import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import config, { DEV_PROXY_PREFIXES } from '../vite.config.js'

// The dev server must reach every backend prefix production's ingress routes
// to the API. With only /api, /auth and /ws proxied, /billing/status was
// answered by index.html and every local workspace opened locked on
// "Workspace verification unavailable".
const APP_SPEC = new URL('../../infra/digitalocean/app.yaml', import.meta.url)

function productionApiPrefixes() {
  const spec = readFileSync(APP_SPEC, 'utf8')
  const ingress = spec.slice(spec.indexOf('\ningress:'))
  const prefixes = []
  let component = null
  for (const line of ingress.split('\n')) {
    const name = line.match(/component:\s*\{\s*name:\s*(\w+)/)
    if (name) component = name[1]
    const prefix = line.match(/prefix:\s*(\/[\w-]*)/)
    if (prefix && component === 'api') prefixes.push(prefix[1])
  }
  return prefixes
}

describe('dev proxy', () => {
  it('proxies every prefix production routes to the API', () => {
    const production = productionApiPrefixes()
    expect(production).toContain('/billing')
    expect([...DEV_PROXY_PREFIXES].sort()).toEqual([...production].sort())
  })

  it('wires each prefix into the dev server, with WebSocket upgrade on /ws only', () => {
    const proxy = config.server.proxy
    expect(Object.keys(proxy).sort()).toEqual([...DEV_PROXY_PREFIXES].sort())
    expect(proxy['/ws'].ws).toBe(true)
    expect(proxy['/billing'].ws).toBe(false)
  })
})
