// A control socket for one VS Code window.
//
// This is `extensionKind: ["ui"]` on purpose. In a Remote-SSH window the
// workspace extension host lives in the seat, which is ephemeral and whose
// filesystem the laptop cannot reach; a UI extension runs in the laptop's
// extension host, so the socket lands where the driving agent actually is and
// survives the seat going away. Everything below still sees the remote
// workspace, because the workbench proxies the API across the two hosts.

const vscode = require('vscode');
const net = require('net');
const fs = require('fs');
const os = require('os');
const path = require('path');

const DIR = path.join(os.homedir(), '.local', 'state', 'podbench-vscode-bridge');

let server = null;
let sockPath = null;
let descPath = null;

// --- event ring -----------------------------------------------------------
// Assertions in an e2e run are mostly "did X happen, and in what order", which
// a poll of current state cannot answer. seq is monotonic and independent of
// the array index so trimming the front does not renumber history.
let seq = 0;
const events = [];
function record(kind, data) {
  events.push({ seq: seq++, t: Date.now(), kind, data });
  if (events.length > 2000) events.splice(0, 1000);
}

// --- serialisation --------------------------------------------------------
// vscode API objects are not JSON: Uri stringifies to something useless
// ({scheme, authority, path, ...} loses the round-trip), and a Range holds
// Positions that hold nothing enumerable. Depth-limit and special-case.
function safe(v, depth) {
  depth = depth || 0;
  if (v === null || v === undefined) return null;
  const t = typeof v;
  if (t === 'string' || t === 'number' || t === 'boolean') return v;
  if (t === 'function') return '[function]';
  if (t === 'symbol' || t === 'bigint') return String(v);
  if (v instanceof vscode.Uri) return v.toString();
  if (v instanceof vscode.Position) return { line: v.line, character: v.character };
  if (v instanceof vscode.Range) {
    return { start: safe(v.start), end: safe(v.end) };
  }
  if (depth > 4) return String(v);
  if (Array.isArray(v)) return v.slice(0, 500).map((x) => safe(x, depth + 1));
  const out = {};
  for (const k of Object.keys(v)) {
    try {
      out[k] = safe(v[k], depth + 1);
    } catch (e) {
      out[k] = '[unreadable]';
    }
  }
  return out;
}

function editorInfo(ed) {
  if (!ed) return null;
  return {
    uri: ed.document.uri.toString(),
    fsPath: ed.document.uri.fsPath,
    languageId: ed.document.languageId,
    lineCount: ed.document.lineCount,
    dirty: ed.document.isDirty,
    selection: safe(ed.selection),
  };
}

function info() {
  const s = vscode.debug.activeDebugSession;
  return {
    pid: process.pid,
    remoteName: vscode.env.remoteName || null,
    appName: vscode.env.appName,
    focused: vscode.window.state.focused,
    folders: (vscode.workspace.workspaceFolders || []).map((f) => ({
      name: f.name,
      uri: f.uri.toString(),
      fsPath: f.uri.fsPath,
    })),
    activeEditor: editorInfo(vscode.window.activeTextEditor),
    visibleEditors: vscode.window.visibleTextEditors.map(editorInfo),
    terminals: vscode.window.terminals.map((t) => ({ name: t.name })),
    debugSession: sessionInfo(s),
    breakpoints: vscode.debug.breakpoints.map(bpInfo),
    seq: seq,
  };
}

// Never `safe()` a DebugSession whole: `configuration` carries the *resolved*
// launch config, which for debugpy includes the full inherited environment --
// tokens, kubeconfig paths, the lot. Three fields are what identifies it.
function sessionInfo(s) {
  return s ? { id: s.id, name: s.name, type: s.type } : null;
}

function bpInfo(bp) {
  const out = { id: bp.id, enabled: bp.enabled };
  if (bp.location) {
    out.uri = bp.location.uri.toString();
    out.fsPath = bp.location.uri.fsPath;
    out.line = bp.location.range.start.line + 1; // 1-based, as a human reads it
  }
  if (bp.functionName) out.functionName = bp.functionName;
  return out;
}

// --- resolving a path against the window ----------------------------------
// A remote window's folder is a vscode-remote:// URI. A caller naming a plain
// path means "in the workspace", not "on the laptop" -- resolving it with
// Uri.file() would silently open a laptop file with the same name, which is
// the whole class of bug this repo's gdb-across-namespaces skill is about.
function resolve(spec) {
  if (spec.indexOf('://') !== -1) return vscode.Uri.parse(spec);
  const folders = vscode.workspace.workspaceFolders || [];
  if (folders.length) {
    const base = folders[0].uri;
    return base.with({ path: spec.charAt(0) === '/' ? spec : base.path + '/' + spec });
  }
  return vscode.Uri.file(spec);
}

// --- operations -----------------------------------------------------------
const ops = {
  async ping() {
    return 'pong';
  },

  async info() {
    return info();
  },

  async commands(req) {
    const all = await vscode.commands.getCommands(true);
    const f = req.filter;
    return (f ? all.filter((c) => c.indexOf(f) !== -1) : all).sort();
  },

  async command(req) {
    const args = req.args || [];
    return safe(await vscode.commands.executeCommand(req.id, ...args));
  },

  async open(req) {
    const uri = resolve(req.uri || req.path);
    const doc = await vscode.workspace.openTextDocument(uri);
    const opts = { preview: req.preview === true };
    if (typeof req.line === 'number') {
      const pos = new vscode.Position(req.line - 1, req.column ? req.column - 1 : 0);
      opts.selection = new vscode.Range(pos, pos);
    }
    const ed = await vscode.window.showTextDocument(doc, opts);
    return editorInfo(ed);
  },

  async text(req) {
    const uri = resolve(req.uri || req.path);
    const doc = await vscode.workspace.openTextDocument(uri);
    if (typeof req.from === 'number' || typeof req.to === 'number') {
      const from = (req.from || 1) - 1;
      const to = req.to ? req.to : doc.lineCount;
      const lines = [];
      for (let i = from; i < Math.min(to, doc.lineCount); i++) lines.push(doc.lineAt(i).text);
      return lines.join('\n');
    }
    return doc.getText();
  },

  async breakpoints(req) {
    if (req.clear) vscode.debug.removeBreakpoints(vscode.debug.breakpoints);
    for (const b of req.set || []) {
      const uri = resolve(b.uri || b.path);
      const pos = new vscode.Position(b.line - 1, 0);
      vscode.debug.addBreakpoints([
        new vscode.SourceBreakpoint(new vscode.Location(uri, pos), true, b.condition),
      ]);
    }
    return vscode.debug.breakpoints.map(bpInfo);
  },

  async debug(req) {
    const folders = vscode.workspace.workspaceFolders || [];
    const folder = folders.length ? folders[0] : undefined;
    const started = await vscode.debug.startDebugging(folder, req.config || req.name);
    return { started: started, session: sessionInfo(vscode.debug.activeDebugSession) };
  },

  // Raw DAP passthrough, so a stopped session can be inspected without this
  // file having to grow an op per request type.
  async dap(req) {
    const s = vscode.debug.activeDebugSession;
    if (!s) throw new Error('no active debug session');
    return safe(await s.customRequest(req.request, req.args || {}));
  },

  // "Where is it stopped, and what is in scope" -- the question an e2e run
  // asks after dap.stopped, and three round trips if done through `dap`.
  async stack(req) {
    const s = vscode.debug.activeDebugSession;
    if (!s) throw new Error('no active debug session');
    const threads = await s.customRequest('threads', {});
    const threadId = req.threadId || (threads.threads[0] || {}).id;
    const trace = await s.customRequest('stackTrace', {
      threadId: threadId,
      levels: req.levels || 10,
    });
    const frames = trace.stackFrames.map((f) => ({
      id: f.id,
      name: f.name,
      line: f.line,
      source: f.source ? f.source.path || f.source.name : null,
    }));
    let variables = null;
    if (frames.length && req.variables !== false) {
      const scopes = await s.customRequest('scopes', { frameId: frames[0].id });
      variables = {};
      for (const sc of scopes.scopes.slice(0, 2)) {
        const vs = await s.customRequest('variables', { variablesReference: sc.variablesReference });
        variables[sc.name] = vs.variables
          .slice(0, 50)
          .map((v) => ({ name: v.name, value: v.value, type: v.type }));
      }
    }
    return { threadId: threadId, frames: frames, variables: variables };
  },

  async terminal(req) {
    let term = vscode.window.terminals.find((t) => t.name === req.name);
    if (!term) term = vscode.window.createTerminal({ name: req.name || 'podbench' });
    term.show(req.preserveFocus !== false);
    if (req.text !== undefined) term.sendText(req.text, req.enter !== false);
    return { name: term.name };
  },

  async diagnostics(req) {
    const all = req.uri
      ? [[resolve(req.uri), vscode.languages.getDiagnostics(resolve(req.uri))]]
      : vscode.languages.getDiagnostics();
    const out = [];
    for (const [uri, ds] of all) {
      for (const d of ds) {
        out.push({
          uri: uri.toString(),
          severity: ['error', 'warning', 'info', 'hint'][d.severity],
          message: d.message,
          line: d.range.start.line + 1,
          source: d.source || null,
        });
      }
    }
    return out;
  },

  async events(req) {
    const since = req.since || 0;
    return events.filter((e) => e.seq >= since);
  },

  // The escape hatch. `code` runs with `vscode`, `ctx` and `require` in scope
  // and is awaited, so anything the API can do is reachable without teaching
  // this file about it first.
  async eval(req) {
    const fn = new Function(
      'vscode',
      'ctx',
      'require',
      '"use strict"; return (async () => { ' + req.code + ' })();',
    );
    return safe(await fn(vscode, ops.__ctx, require));
  },
};

// --- descriptor -----------------------------------------------------------
// The point of writing this file is that an agent with several windows open
// can tell them apart *before* connecting: remoteName and folders are what
// identify "the window `podbench vscode` just opened".
function writeDescriptor() {
  const d = {
    pid: process.pid,
    socket: sockPath,
    remoteName: vscode.env.remoteName || null,
    folders: (vscode.workspace.workspaceFolders || []).map((f) => f.uri.toString()),
    started: Date.now(),
  };
  fs.writeFileSync(descPath, JSON.stringify(d, null, 2));
}

function activate(ctx) {
  ops.__ctx = ctx;
  fs.mkdirSync(DIR, { recursive: true, mode: 0o700 });
  sockPath = path.join(DIR, process.pid + '.sock');
  descPath = path.join(DIR, process.pid + '.json');
  try {
    fs.unlinkSync(sockPath);
  } catch (e) {
    /* first run */
  }

  server = net.createServer((conn) => {
    let buf = '';
    conn.on('data', async (chunk) => {
      buf += chunk.toString('utf8');
      let nl;
      while ((nl = buf.indexOf('\n')) !== -1) {
        const line = buf.slice(0, nl);
        buf = buf.slice(nl + 1);
        if (!line.trim()) continue;
        let reply;
        try {
          const req = JSON.parse(line);
          const op = ops[req.op];
          if (!op) throw new Error('unknown op: ' + req.op);
          reply = { ok: true, result: await op(req) };
        } catch (err) {
          reply = { ok: false, error: String(err && err.message ? err.message : err) };
          if (err && err.stack) reply.stack = err.stack.split('\n').slice(0, 6).join('\n');
        }
        conn.write(JSON.stringify(reply) + '\n');
      }
    });
    conn.on('error', () => {});
  });
  server.listen(sockPath, writeDescriptor);

  const sub = ctx.subscriptions;
  sub.push(
    vscode.workspace.onDidChangeWorkspaceFolders(writeDescriptor),
    vscode.debug.onDidStartDebugSession((s) =>
      record('debug.start', { id: s.id, name: s.name, type: s.type }),
    ),
    vscode.debug.onDidTerminateDebugSession((s) => record('debug.end', { id: s.id, name: s.name })),
    vscode.debug.onDidChangeActiveDebugSession((s) =>
      record('debug.active', s ? { id: s.id, name: s.name } : null),
    ),
    vscode.debug.onDidChangeBreakpoints((e) =>
      record('breakpoints', {
        added: e.added.map(bpInfo),
        removed: e.removed.map(bpInfo),
        changed: e.changed.map(bpInfo),
      }),
    ),
    // A stopped event is what "the breakpoint bound and hit" actually looks
    // like; the debug API exposes it only through a tracker.
    vscode.debug.registerDebugAdapterTrackerFactory('*', {
      createDebugAdapterTracker(session) {
        return {
          onDidSendMessage(m) {
            if (m.type === 'event' && (m.event === 'stopped' || m.event === 'terminated')) {
              record('dap.' + m.event, { session: session.name, body: safe(m.body) });
            }
          },
        };
      },
    }),
    vscode.window.onDidOpenTerminal((t) => record('terminal.open', { name: t.name })),
    vscode.window.onDidChangeActiveTextEditor((e) =>
      record('editor.active', e ? { uri: e.document.uri.toString() } : null),
    ),
  );
  record('bridge.ready', { socket: sockPath, remoteName: vscode.env.remoteName || null });
}

function deactivate() {
  if (server) server.close();
  for (const p of [sockPath, descPath]) {
    try {
      if (p) fs.unlinkSync(p);
    } catch (e) {
      /* already gone */
    }
  }
}

module.exports = { activate, deactivate };
