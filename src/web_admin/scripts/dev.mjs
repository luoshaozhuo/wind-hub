import { spawn } from 'node:child_process'
import { fileURLToPath } from 'node:url'

const args = process.argv.slice(2)
const mock = args[0] === 'mock'
const vite = fileURLToPath(new URL('../node_modules/vite/bin/vite.js', import.meta.url))
const child = spawn(process.execPath, [vite, ...(mock ? ['--mode', 'mock'] : []), ...(mock ? args.slice(1) : args)], { stdio: 'inherit' })
child.on('error', error => { console.error(error); process.exitCode = 1 })
child.on('exit', code => { process.exitCode = code ?? 1 })
