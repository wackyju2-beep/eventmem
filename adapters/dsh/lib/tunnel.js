/**
 * eventmem / dsh — the SSH tunnel supervisor.
 *
 * Cross-machine shape: the daemon only listens on its own loopback, so this side needs
 * `ssh -N -L <local>:127.0.0.1:<remote> <host>`. Owning that process inside the plugin is
 * what makes the tunnel follow the Harness: it starts with the adapter's activation and
 * is terminated when the adapter is disposed (plugin disabled, uninstalled, reloaded).
 *
 * Deliberate behaviours:
 *  - an already-working tunnel (autossh, a manual `ssh -N`, a leftover from a hard kill)
 *    is ADOPTED, never fought over: binding a second ssh to the same port would just fail;
 *  - an ssh this supervisor started is killed on stop; an adopted one is not, because we
 *    do not own it;
 *  - losing the forwarding never throws: the memory hooks already degrade to "no memory".
 */

import { spawn } from 'node:child_process'

/** Forwarding options that matter for an unattended tunnel. */
function defaultArgs(cfg) {
  return [
    '-N',
    '-o', 'BatchMode=yes',
    '-o', 'ExitOnForwardFailure=yes',
    '-o', 'ServerAliveInterval=30',
    '-o', 'ServerAliveCountMax=3',
    '-L', `${cfg.tunnelLocalPort}:127.0.0.1:${cfg.tunnelRemotePort}`,
    cfg.tunnelHost,
  ]
}

export class TunnelSupervisor {
  /**
   * @param {{cfg: any, probe: () => Promise<unknown>, notify: (level: string, message: string) => void}} options
   */
  constructor(options) {
    this.cfg = options.cfg
    this.probe = options.probe
    this.notify = options.notify
    this.child = null
    this.timer = null
    this.verifyTimer = null
    this.retryTimer = null
    this.checking = false
    this.stopped = false
    this.attempt = 0
    this.nextAt = 0
    this.graceUntil = 0
    this.lastState = ''
  }

  /** Only report a state change once; a down tunnel must not flood the log. */
  state(level, message) {
    if (message === this.lastState) return
    this.lastState = message
    this.notify(level, message)
  }

  start() {
    this.stopped = false
    this.check().catch(() => {})
    this.timer = setInterval(() => {
      this.check().catch(() => {})
    }, this.cfg.tunnelIntervalMs)
    if (typeof this.timer.unref === 'function') this.timer.unref()
  }

  stop() {
    this.stopped = true
    if (this.timer !== null) {
      clearInterval(this.timer)
      this.timer = null
    }
    if (this.verifyTimer !== null) {
      clearTimeout(this.verifyTimer)
      this.verifyTimer = null
    }
    if (this.retryTimer !== null) {
      clearTimeout(this.retryTimer)
      this.retryTimer = null
    }
    const child = this.child
    this.child = null
    if (child === null) return
    try {
      child.removeAllListeners('exit')
      child.kill()
      this.notify('info', `stopped tunnel (ssh pid ${child.pid})`)
    } catch (error) {
      this.notify('warn', `could not stop tunnel pid ${child.pid}: ${error && error.message ? error.message : error}`)
    }
  }

  async check() {
    if (this.stopped || this.checking || !this.cfg.tunnelHost) return
    if (Date.now() < this.nextAt) return
    if (this.child !== null) {
      // Our ssh is alive. Confirm the data plane once per spawn, but never kill the
      // forwarding because the daemon happens to be down — that is the daemon's problem.
      if (this.graceUntil !== 0 && Date.now() >= this.graceUntil) {
        this.graceUntil = 0
        const health = await this.probe()
        if (health) this.state('info', `tunnel ready (ssh pid ${this.child.pid})`)
        else this.state('warn', `tunnel ssh pid ${this.child.pid} is up but the data plane is unreachable (daemon down?)`)
      }
      return
    }

    this.checking = true
    try {
      const health = await this.probe()
      if (health) {
        this.attempt = 0
        this.state('info', `using an existing tunnel to ${this.cfg.baseUrl}`)
        return
      }
      this.spawnTunnel()
    } finally {
      this.checking = false
    }
  }

  spawnTunnel() {
    if (this.stopped || this.child !== null) return
    const args = [...defaultArgs(this.cfg), ...(Array.isArray(this.cfg.tunnelArgs) ? this.cfg.tunnelArgs : [])]
    let child
    try {
      // stdin stays an open pipe on purpose: `ssh -N` exits on stdin EOF, so when the
      // Harness process dies (even hard), the pipe closes and the orphan cleans itself up.
      child = spawn(this.cfg.tunnelSsh, args, { stdio: ['pipe', 'ignore', 'ignore'], windowsHide: true })
    } catch (error) {
      this.backoff(`could not spawn ${this.cfg.tunnelSsh}: ${error && error.message ? error.message : error}`)
      return
    }
    this.child = child
    this.graceUntil = Date.now() + 2500
    this.state('info', `started tunnel (ssh pid ${child.pid}): ${this.cfg.tunnelSsh} ${args.join(' ')}`)

    child.on('error', (error) => {
      if (this.child !== child) return
      this.child = null
      this.backoff(`ssh failed: ${error && error.message ? error.message : error}`)
    })
    child.on('exit', (code, signal) => {
      if (this.child !== child) return
      this.child = null
      if (this.stopped) return
      this.backoff(`ssh exited (code=${String(code)}, signal=${String(signal)})`)
    })

    // Confirm the forwarding shortly after spawn instead of waiting for the next tick.
    this.verifyTimer = setTimeout(() => {
      this.check().catch(() => {})
    }, 2800)
    if (typeof this.verifyTimer.unref === 'function') this.verifyTimer.unref()
  }

  /** Bounded exponential backoff, so a missing user/host cannot spin. */
  backoff(reason) {
    this.attempt = Math.min(this.attempt + 1, 5)
    const delay = Math.min(this.cfg.tunnelIntervalMs, 2000 * 2 ** (this.attempt - 1))
    this.nextAt = Date.now() + delay
    this.state('warn', `${reason}; retrying in ${delay}ms`)
    if (this.retryTimer !== null) clearTimeout(this.retryTimer)
    this.retryTimer = setTimeout(() => {
      this.check().catch(() => {})
    }, delay)
    if (typeof this.retryTimer.unref === 'function') this.retryTimer.unref()
  }
}
