/**
 * DSH 桥端到端回归（无需启动 DSH 主程序）
 *
 * 用 mock ctx 加载真实的 dsh-deskpet-bridge/index.js，走完整 WebSocket 链路：
 *   test1  握手 + welcome + ping/pong
 *   test2  桌宠 → DSH：{type:'send'} → agent.followup()
 *   test3  DSH → 桌宠：running → token×3 → stream-end → done(turn/end)
 *   test4  GET /health 契约（指示灯依赖：ok/sockets/events/lastAt 为 ISO 字符串）
 *
 * 用法（任意工作目录均可，路径按本文件位置解析）：
 *   node deskpet-python/build/tools/harness_bridge_test.mjs
 *   node deskpet-python/build/tools/harness_bridge_test.mjs 59123
 *   DESHPET_HARNESS_PORT=59123 node deskpet-python/build/tools/harness_bridge_test.mjs
 *
 * 端口默认向系统要一个空闲端口，不与真实插件的 55471 冲突；显式指定时请确保没被占用。
 * 退出码：四项全 PASS 为 0，否则 1。
 */
import net from 'node:net';
import crypto from 'node:crypto';
import { get as httpGet } from 'node:http';
import { apply } from '../../../dsh-deskpet-bridge/index.js';

async function pickFreePort() {
  return new Promise((resolve, reject) => {
    const s = net.createServer();
    s.once('error', reject);
    s.listen(0, '127.0.0.1', () => {
      const p = s.address().port;
      s.close(() => resolve(p));
    });
  });
}

const wanted = Number(process.env.DESHPET_HARNESS_PORT || process.argv[2] || 0);
const PORT = wanted || await pickFreePort();
const captured = [];
const disposers = [];

// ---- mock DSH ctx -----------------------------------------------------------
// currentInitiator() 必须返回 undefined 而不是抛错：插件 resolveAgent() 用一整段
// try 包住三种取 agent 的方式，任一抛错会连 roots() 一起跳过，followup 永远不触发。
const fakeAgent = {
  id: 'root-1',
  sessionId: 'root-1',
  followup(message) {
    captured.push(message);
  },
};
const handlers = {}; // event name -> [{fn, opts}]
const ctx = {
  agents: {
    get() { return undefined; },
    currentInitiator() { return undefined; },
    roots() { return [fakeAgent]; },
  },
  on(name, fn, opts) {
    (handlers[name] ||= []).push({ fn, opts });
    return () => {};
  },
  // ctx.effect(execute) 立即执行 execute，返回值才是 disposer。这里只收集、不执行，
  // 收尾时统一调用，顺便验证插件的清理路径确实返回了函数。
  effect(execute) {
    try {
      const d = execute();
      if (typeof d === 'function') disposers.push(d);
    } catch { /* 不让 mock 影响被测代码 */ }
    return () => {};
  },
  logger: {
    info: () => {},
    error: (m) => console.log('[bridge.error]', m),
    warn: (m) => console.log('[bridge.warn]', m),
  },
};

apply(ctx, { port: PORT, debug: false });
await new Promise((r) => setTimeout(r, 300)); // 等 listen 落地

// 触发宿主事件。session/event 的签名是 (session, event) 两参，故用 ...args。
function fire(name, ...args) {
  for (const h of handlers[name] || []) h.fn(...args);
}

function sendJson(sock, obj) {
  const p = Buffer.from(JSON.stringify(obj));
  const mask = crypto.randomBytes(4);
  const m = Buffer.alloc(p.length);
  for (let i = 0; i < p.length; i++) m[i] = p[i] ^ mask[i & 3];
  sock.write(Buffer.concat([Buffer.from([0x81, 0x80 | p.length]), mask, m]));
}

// ---- raw WS client（握手 + 帧解析，不依赖第三方 ws 包）----------------------
function client(handshakeThen) {
  return new Promise((resolve) => {
    const key = crypto.randomBytes(16).toString('base64');
    const accept = crypto.createHash('sha1')
      .update(key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest('base64');
    const sock = net.connect(PORT, '127.0.0.1', () => {
      sock.write(
        'GET / HTTP/1.1\r\nHost: 127.0.0.1:' + PORT + '\r\n' +
        'Upgrade: websocket\r\nConnection: Upgrade\r\n' +
        'Sec-WebSocket-Key: ' + key + '\r\nSec-WebSocket-Version: 13\r\n\r\n'
      );
    });
    let done = false;
    let handshakeDone = false;
    let buf = Buffer.alloc(0);
    const received = [];
    const finish = (ok, note) => {
      if (done) return;
      done = true;
      sock.destroy();
      resolve({ ok, note, received });
    };
    function parse() {
      while (buf.length >= 2) {
        const b1 = buf[1];
        let len = b1 & 0x7f;
        let off = 2;
        if (len === 126) { if (buf.length < 4) return; len = buf.readUInt16BE(2); off = 4; }
        else if (len === 127) { if (buf.length < 10) return; len = Number(buf.readBigUInt64BE(2)); off = 10; }
        if (buf.length < off + len) return;
        received.push(buf.subarray(off, off + len).toString('utf8'));
        buf = buf.subarray(off + len);
      }
    }
    sock.on('data', (chunk) => {
      if (!handshakeDone) {
        const s = chunk.toString('utf8');
        const idx = s.indexOf('\r\n\r\n');
        if (idx === -1) return;
        const header = s.slice(0, idx);
        const rest = chunk.subarray(idx + 4);
        if (!/101 Switching Protocols/.test(header)) {
          return finish(false, 'handshake not 101: ' + header.split('\r\n')[0]);
        }
        if (!header.includes('Sec-WebSocket-Accept: ' + accept)) {
          return finish(false, 'accept mismatch');
        }
        handshakeDone = true;
        if (rest.length) { buf = Buffer.from(rest); parse(); }
        handshakeThen(sock, finish, received);
        return;
      }
      buf = Buffer.concat([buf, chunk]);
      parse();
    });
    sock.on('error', (e) => finish(false, 'sock error: ' + e.message));
    setTimeout(() => finish(false, 'timeout'), 2000);
  });
}

// test1: 握手 + welcome + ping/pong
const r1 = await client((sock, finish, received) => {
  const welcome = received.find((f) => f.includes('"welcome"'));
  if (!welcome) return finish(false, 'no welcome frame');
  console.log('welcome:', welcome.trim());
  sendJson(sock, { type: 'ping' });
  setTimeout(() => {
    const pong = received.find((f) => f.includes('"pong"'));
    if (!pong) return finish(false, 'no pong after ping');
    console.log('pong:', pong.trim());
    finish(true, 'handshake+welcome+ping/pong OK');
  }, 200);
});

// test2: 桌宠 → DSH，send 必须落到 agent.followup()
const r2 = await client((sock, finish) => {
  sendJson(sock, { type: 'send', text: '你好桌宠' });
  setTimeout(() => {
    const last = captured[captured.length - 1];
    if (!last || last.content?.[0]?.text !== '你好桌宠') {
      return finish(false, 'followup not called with text; captured=' + JSON.stringify(captured));
    }
    console.log('followup captured role=' + last.role + ' text=' + last.content[0].text);
    finish(true, 'send->followup OK');
  }, 200);
});

// test3: DSH → 桌宠。
// 注意：frame.type='end' 现在只广播 stream-end；真正的 done 由 session/event 的
// turn/end 触发（index.js:440-442 / 465-466）。旧 harness 只断言 done 因此恒 FAIL。
const r3 = await client((sock, finish, received) => {
  const seen = () => received.filter((f) =>
    f.includes('"token"') || f.includes('"done"') || f.includes('"status"') || f.includes('"stream-end"'));
  fire('agent/status', { agent: fakeAgent, status: 'running' });
  for (const text of ['你', '好', '呀']) {
    fire('agent/assistant-stream', {
      agent: fakeAgent,
      frame: { type: 'chunk', chunk: { type: 'text-delta', text } },
    });
  }
  fire('agent/assistant-stream', { agent: fakeAgent, frame: { type: 'end' } });
  fire('session/event', { session: fakeAgent }, { type: 'turn/end' });
  setTimeout(() => {
    const tokens = received.filter((f) => f.includes('"token"'));
    const running = received.find((f) => f.includes('"status"') && f.includes('running'));
    const streamEnd = received.find((f) => f.includes('"stream-end"'));
    const done = received.find((f) => f.includes('"done"'));
    if (!running) return finish(false, 'no running status frame');
    if (tokens.length !== 3) {
      return finish(false, 'expected 3 tokens, got ' + tokens.length + ' :: ' + JSON.stringify(seen()));
    }
    if (!streamEnd) return finish(false, 'no stream-end frame');
    if (!done) return finish(false, 'no done frame (session/event turn/end)');
    console.log('stream frames:', tokens.map((t) => t.trim()).join(' '),
      '| stream-end | done:' + done.trim());
    finish(true, 'DSH->pet streaming OK (3 tokens + stream-end + done)');
  }, 200);
});

// test4: /health 契约（设置窗 DSH 指示灯的数据来源）
const r4 = await new Promise((resolve) => {
  httpGet(`http://127.0.0.1:${PORT}/health`, (res) => {
    let body = '';
    res.on('data', (c) => { body += c; });
    res.on('end', () => {
      try {
        const h = JSON.parse(body);
        const ok = res.statusCode === 200 && h.ok === true
          && typeof h.events === 'number' && typeof h.sockets === 'number'
          && (h.lastAt === null || /^\d{4}-\d{2}-\d{2}T.*Z$/.test(h.lastAt));
        resolve({ ok, note: ok ? '/health OK (lastAt ISO)' : '/health 契约不符: ' + body, received: [body] });
      } catch (e) {
        resolve({ ok: false, note: '/health JSON 解析失败: ' + e.message, received: [body] });
      }
    });
  }).on('error', (e) => resolve({ ok: false, note: '/health 请求失败: ' + e.message, received: [] }));
});

const results = [r1, r2, r3, r4];
console.log('\n=== RESULT (port ' + PORT + ') ===');
results.forEach((r, i) => console.log(`test${i + 1}:`, r.ok ? 'PASS' : 'FAIL', '-', r.note));
const allOk = results.every((r) => r.ok);
for (const d of disposers) { try { d(); } catch { /* ignore */ } }
console.log('disposers returned by ctx.effect =', disposers.length);
process.exit(allOk ? 0 : 1);
