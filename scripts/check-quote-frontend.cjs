// Used by detached Windows startup scripts; verify the entry page and its API proxy.
async function main() {
  const port = Number(process.argv[2])
  if (!Number.isInteger(port) || port < 1 || port > 65535) process.exit(2)
  const base = `http://127.0.0.1:${port}`
  const [entry, api] = await Promise.all([
    fetch(base, { signal: AbortSignal.timeout(2500) }),
    fetch(`${base}/api/health`, { signal: AbortSignal.timeout(2500) }),
  ])
  if (!entry.ok || !api.ok || !(await entry.text()).includes('/src/main.ts')
    || (await api.json()).status !== 'ok') process.exitCode = 1
}
main().catch(() => { process.exitCode = 1 })
