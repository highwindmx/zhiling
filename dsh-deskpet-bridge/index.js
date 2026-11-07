import { createHash, randomUUID } from 'node:crypto';
import { appendFileSync, existsSync, mkdirSync, renameSync, statSync, unlinkSync } from 'node:fs';
import { createServer } from 'node:http';
import { dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const WS_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11';

// 独立的加载/监听日志：DSH 的 ctx.logger 只在“已挂载”后才有输出，
// 用它区分“模块没被执行”和“执行了但端口失败”。
const PLUGIN_LOG = process.env.DESHPET_PLUGIN_LOG
  || fileURLToPath(new URL('../deskpet-python/build/cache/dsh_bridge_plugin.log', import.meta.url));

const LOG_MAX_BYTES = 2 * 1024 * 1024;   // 单文件上限 2 MB；归档固定 .1，只留 1 份
let logBytes = -1;                        // -1 = 尚未从磁盘读取（跨进程重启后继续按真实大小轮转）
let rotateWarned = false;

function rotateLog() {
  const bak = `${PLUGIN_LOG}.1`;
  try {
    if (existsSync(bak)) unlinkSync(bak);
    renameSync(PLUGIN_LOG, bak);
    logBytes = 0;
  } catch (e) {
    if (!rotateWarned) {
      rotateWarned = true;
      try {
        appendFileSync(PLUGIN_LOG, `[log] rotate failed: ${(e && e.message) || e}\n`);
      } catch { /* ignore */ }
    }
  }
}

function plog(line) {
  try {
    mkdirSync(dirname(PLUGIN_LOG), { recursive: true });
    if (logBytes < 0) {
      try { logBytes = statSync(PLUGIN_LOG).size; } catch { logBytes = 0; }
    }
    if (logBytes > LOG_MAX_BYTES) rotateLog();
    const text = `[${new Date().toISOString()}] ${line}\n`;
    appendFileSync(PLUGIN_LOG, text);
    logBytes += Buffer.byteLength(text);
  } catch {
    /* 日志失败不影响插件 */
  }
}
plog(`module loaded v2 pid=${process.pid} node=${process.version} file=${import.meta.url}`);

// ---- tiny zero-dependency WebSocket server (RFC 6455) -----------------------
function makeAcceptValue(key) {
  return createHash('sha1').update(key + WS_GUID).digest('base64');
}

function sendFrame(socket, data, opcode = 0x1) {
  if (socket.destroyed) return;
  const payload = Buffer.isBuffer(data) ? data : Buffer.from(typeof data === 'string' ? data : JSON.stringify(data));
  const len = payload.length;
  let header;
  if (len < 126) {
    header = Buffer.from([0x80 | opcode, len]);
  } else if (len < 65536) {
    header = Buffer.alloc(4);
    header[0] = 0x80 | opcode;
    header[1] = 126;
    header.writeUInt16BE(len, 2);
  } else {
    header = Buffer.alloc(10);
    header[0] = 0x80 | opcode;
    header[1] = 127;
    header.writeBigUInt64BE(BigInt(len), 2);
  }
  try {
    socket.write(Buffer.concat([header, payload]));
  } catch {
    /* ignore write errors on a closing socket */
  }
}

function parseFrames(state, chunk) {
  state.buf = state.buf && state.buf.length ? Buffer.concat([state.buf, chunk]) : chunk;
  const messages = [];
  for (;;) {
    const buf = state.buf;
    if (buf.length < 2) break;
    const b0 = buf[0];
    const b1 = buf[1];
    const fin = (b0 & 0x80) !== 0;
    const opcode = b0 & 0x0f;
    const masked = (b1 & 0x80) !== 0;
    let len = b1 & 0x7f;
    let offset = 2;
    if (len === 126) {
      if (buf.length < offset + 2) break;
      len = buf.readUInt16BE(offset);
      offset += 2;
    } else if (len === 127) {
      if (buf.length < offset + 8) break;
      len = Number(buf.readBigUInt64BE(offset));
      offset += 8;
    }
    let maskKey;
    if (masked) {
      if (buf.length < offset + 4) break;
      maskKey = buf.subarray(offset, offset + 4);
      offset += 4;
    }
    if (buf.length < offset + len) break;
    if (len > 64 * 1024 * 1024) {
      state.buf = Buffer.alloc(0);
      state.dead = true;
      return { messages, dead: true };
    }
    let payload = buf.subarray(offset, offset + len);
    if (masked) {
      const out = Buffer.allocUnsafe(len);
      for (let i = 0; i < len; i++) out[i] = payload[i] ^ maskKey[i & 3];
      payload = out;
    } else {
      payload = Buffer.from(payload);
    }
    state.buf = buf.subarray(offset + len);

    if (opcode === 0x8) {
      state.dead = true;
      return { messages, dead: true };
    } else if (opcode === 0x9) {
      sendFrame(state.socket, payload, 0xa);
      continue;
    } else if (opcode === 0xa) {
      continue;
    }
    if (opcode === 0x0) {
      state.frag = state.frag ? Buffer.concat([state.frag, payload]) : payload;
    } else {
      state.frag = payload;
      state.fragOp = opcode;
    }
    if (fin) {
      messages.push({ data: state.frag, opcode: state.fragOp });
      state.frag = null;
      state.fragOp = null;
    }
  }
  return { messages, dead: false };
}

// Local mirror of @deepseek-ai/dsh-llm's createUserMessage.
function deepFreeze(value) {
  if (value && typeof value === 'object' && !Object.isFrozen(value)) {
    Object.freeze(value);
    for (const key of Reflect.ownKeys(value)) {
      const v = value[key];
      if (v && typeof v === 'object') deepFreeze(v);
    }
  }
  return value;
}

function createUserMessage(input) {
  const message = {
    id: randomUUID(),
    role: 'user',
    ...input,
  };
  return deepFreeze(structuredClone(message));
}

// ---- helpers -------------------------------------------------------------
// cordis 的 ctx 是 Proxy：读**未声明 inject** 的属性会直接抛
// `cannot get property "xxx" without inject`，而且异常会从监听器冒泡回
// 触发方（例如新建会话）。所以任何来自事件 payload 的对象（agent/session/
// event 都可能是 ctx 代理）都只能“安全读”，绝不直接点属性。
function safeGet(obj, key) {
  try {
    return obj == null ? undefined : obj[key];
  } catch {
    return undefined;
  }
}
function short(v, n = 200) {
  let s;
  try {
    s = typeof v === 'string' ? v : JSON.stringify(v);
  } catch {
    s = '[unserializable]';
  }
  if (typeof s !== 'string') s = String(s);
  return s.length > n ? s.slice(0, n) + `…(${s.length})` : s;
}
function keysOf(v) {
  try {
    return v && typeof v === 'object' ? Object.keys(v).join(',') : typeof v;
  } catch {
    return typeof v;
  }
}
const sessionIdOf = (agent) => {
  if (!agent) return undefined;
  for (const key of ['sessionId', 'id']) {
    const v = safeGet(agent, key);
    if (typeof v === 'string' && v) return v;
  }
  const session = safeGet(agent, 'session');
  const sid = safeGet(session, 'id');
  return typeof sid === 'string' && sid ? sid : undefined;
};

// ---- plugin ----------------------------------------------------------------
export const inject = ['agents'];

const DEFAULT_PORT = 55471;

/**
 * v2 设计要点：**先监听端口，再碰 ctx**。
 * v1 在 listen 之前调用 resolveAgent(ctx.agents)，若该版本 harness 的 agents
 * 接口形状不同（例如 roots 不是函数）会抛异常，apply 直接中断 —— 表现就是
 * 日志里只有 `apply called`、没有 `listening`、55471 也不监听。
 * 现在监听与订阅彼此独立：订阅失败只会少事件，不会让端口起不来。
 */
export function apply(ctx, config) {
  config = { port: DEFAULT_PORT, allowRemote: false, debug: false, ...(config || {}) };
  const port = Number(config.port) || DEFAULT_PORT;
  const state = { started: Date.now(), events: 0, tokens: 0, sockets: 0, lastType: null, lastAt: null };
  plog(`apply called v3 pid=${process.pid} port=${port} keys=${keysOf(ctx)}`);

  const sockets = new Set();
  const broadcast = (obj) => {
    state.events++;
    state.lastType = obj.type;
    state.lastAt = Date.now();
    const msg = JSON.stringify(obj);
    for (const s of sockets) sendFrame(s, msg, 0x1);
    if (config.debug && obj.type !== 'token') plog(`broadcast ${short(obj)}`);
  };

  // ---- 1) 端口先起来 ------------------------------------------------------
  let server;
  try {
    server = createServer((req, res) => {
      // GET /health：不依赖 WS 也能确认“插件活着、收到过多少事件”
      const body = JSON.stringify({
        ok: true,
        plugin: '@local/dsh-deskpet-bridge',
        version: 3,
        pid: process.pid,
        port,
        uptimeSec: Math.round((Date.now() - state.started) / 1000),
        sockets: sockets.size,
        events: state.events,
        tokens: state.tokens,
        lastType: state.lastType,
        lastAt: state.lastAt ? new Date(state.lastAt).toISOString() : null,
      });
      res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' });
      res.end(body);
    });
    server.on('upgrade', (req, socket, head) => {
      if (!config.allowRemote) {
        const raw = req.socket.remoteAddress || '';
        const ip = raw.replace(/^::ffff:/, '').replace(/^::1$/, '127.0.0.1');
        if (ip !== '127.0.0.1' && ip !== '::1' && ip !== 'localhost') {
          socket.destroy();
          return;
        }
      }
      if ((req.headers.upgrade || '').toLowerCase() !== 'websocket') {
        socket.destroy();
        return;
      }
      const key = req.headers['sec-websocket-key'];
      if (!key) {
        socket.destroy();
        return;
      }
      socket.write(
        'HTTP/1.1 101 Switching Protocols\r\n' +
          'Upgrade: websocket\r\n' +
          'Connection: Upgrade\r\n' +
          `Sec-WebSocket-Accept: ${makeAcceptValue(key)}\r\n\r\n`
      );

      const st = { buf: Buffer.alloc(0), frag: null, fragOp: null, socket, dead: false };
      sockets.add(socket);
      socket.setTimeout(0);
      try {
        sendFrame(socket, JSON.stringify({ type: 'welcome', origin: `ws://127.0.0.1:${port}`, version: 2 }));
      } catch {
        /* ignore */
      }
      plog(`ws client connected sockets=${sockets.size}`);

      const onData = (chunk) => {
        const { messages, dead } = parseFrames(st, chunk);
        for (const m of messages) onMessage(m.data);
        if (dead) {
          sockets.delete(socket);
          try {
            socket.destroy();
          } catch {
            /* ignore */
          }
        }
      };
      socket.on('data', onData);
      const cleanup = () => {
        if (sockets.has(socket)) {
          sockets.delete(socket);
          plog(`ws client gone sockets=${sockets.size}`);
        }
      };
      socket.on('error', cleanup);
      socket.on('close', cleanup);
      socket.on('end', cleanup);

      if (head && head.length) onData(head);
    });
    server.on('error', (e) => {
      plog(`server error on 127.0.0.1:${port}: ${e && e.message}`);
      try {
        ctx.logger && ctx.logger.error(`[deskpet-bridge] error: ${e && e.message}`);
      } catch {
        /* ignore */
      }
    });
    server.on('listening', () => plog(`listening event ws://127.0.0.1:${port}`));
    server.listen(port, '127.0.0.1', () => {
      plog(`listening on ws://127.0.0.1:${port} pid=${process.pid}`);
      try {
        ctx.logger.info(`[deskpet-bridge] listening on ws://127.0.0.1:${port}`);
      } catch {
        /* logger 可能还没有 */
      }
    });
    try {
      // 注意：ctx.effect(execute) 是“立即执行 execute，把它的返回值当 disposer”，
      // 所以 execute 必须 return 清理函数；直接在里面 close() 会在 apply 阶段就关掉 server。
      ctx.effect(() => () => {
        try {
          server.close();
        } catch {
          /* ignore */
        }
        plog('server closed by effect dispose');
      });
    } catch (e) {
      plog(`ctx.effect unavailable (不影响监听): ${e && e.message}`);
    }
  } catch (e) {
    plog(`server init FAILED: ${e && e.message}\n${e && e.stack}`);
  }

  // ---- 2) 向 DSH 发消息（桌宠 → DSH） ------------------------------------
  const resolveAgent = () => {
    try {
      // ctx 是 Proxy：即使 agents 已写进 inject，也统一走 safeGet + 能力探测，
      // 不同版本的 ctx 形状变化时只会“取不到”，不会抛。
      const agents = safeGet(ctx, 'agents');
      if (!agents || typeof agents !== 'object') return undefined;
      const get = safeGet(agents, 'get');
      if (config.sessionId && typeof get === 'function') {
        const byId = get.call(agents, config.sessionId);
        if (byId) return byId;
      }
      const currentInitiator = safeGet(agents, 'currentInitiator');
      if (typeof currentInitiator === 'function') {
        const cur = currentInitiator.call(agents);
        if (cur) return cur;
      }
      const rootsFn = safeGet(agents, 'roots');
      if (typeof rootsFn === 'function') {
        const roots = rootsFn.call(agents);
        if (roots && roots.length) return roots[0];
      }
    } catch (e) {
      plog(`resolveAgent failed: ${e && e.message}`);
    }
    return undefined;
  };

  const handleSend = async (text) => {
    if (typeof text !== 'string' || !text.trim()) return;
    const agent = resolveAgent();
    if (!agent) {
      broadcast({ type: 'error', message: 'no active session' });
      return;
    }
    try {
      const message = createUserMessage({
        content: [{ type: 'text', text }],
        source: { kind: 'user' },
      });
      agent.followup(message);
      plog(`send ok len=${text.length}`);
    } catch (e) {
      broadcast({ type: 'error', message: String((e && e.message) || e) });
      plog(`send failed: ${e && e.message}`);
    }
  };

  const onMessage = (buf) => {
    let msg;
    try {
      msg = JSON.parse(buf.toString('utf8'));
    } catch {
      return;
    }
    if (!msg || typeof msg !== 'object') return;
    if (msg.type === 'send') {
      void handleSend(msg.text);
    } else if (msg.type === 'ping') {
      broadcast({ type: 'pong' });
    }
  };

  // ---- 3) 事件订阅（每一个都单独 try/catch，绝不拖垮 apply） -------------
  const sampleCount = {};
  const sample = (name, payload) => {
    sampleCount[name] = (sampleCount[name] || 0) + 1;
    if (sampleCount[name] <= 3) plog(`evt#${sampleCount[name]} ${name} keys=${keysOf(payload)} body=${short(payload, 300)}`);
  };
  const sub = (name, fn) => {
    // 兼容两种 ctx.on：带 {global:true} 的与不带第三参的；都失败才算订阅失败
    let lastErr;
    for (const opts of [{ global: true }, undefined]) {
      try {
        const off = opts ? ctx.on(name, fn, opts) : ctx.on(name, fn);
        plog(`subscribe ok ${name}${opts ? ' (global)' : ''}`);
        return typeof off === 'function' ? off : () => {};
      } catch (e) {
        lastErr = e;
      }
    }
    plog(`subscribe FAIL ${name}: ${lastErr && lastErr.message}`);
    return () => {};
  };

  // ---- 2.5) 待决策信号：只提醒，不代答 -----------------------------------
  // 两类“等人决策”都旁听 session/event，不订阅 user-questions/request 与
  // approval/request 的 waterfall（那会接管决策，桥只做提醒）：
  //   a) 选择题：tool/call 里 name 精确等于 ask_user_question（arguments.questions[]）
  //      -> question-asked / question-cleared
  //   b) 审批：approval/asked（工具要用户点「允许/拒绝」）
  //      -> approval-asked / approval-decided
  // 两者事件形状不同：选择题带 questions[]/options[]，审批只有 id/toolName/reason。
  const pendingQuestions = new Map();          // callId -> { sessionId }
  const pendingApprovals = new Map();          // approval id -> { sessionId }
  const parseQuestionArgs = (raw) => {
    let parsed = raw;
    if (typeof raw === 'string') {
      try {
        parsed = JSON.parse(raw);
      } catch {
        return [];
      }
    }
    const list = safeGet(parsed, 'questions');
    return Array.isArray(list) ? list : [];
  };
  const askPayload = (callId, sid, questions) => {
    const items = questions.map((q) => {
      const rawOptions = safeGet(q, 'options');
      const options = (Array.isArray(rawOptions) ? rawOptions : []).map((o) => ({
        label: safeGet(o, 'label'),
        ...(safeGet(o, 'description') === undefined ? {} : { description: safeGet(o, 'description') }),
      }));
      return {
        id: safeGet(q, 'id'),
        header: safeGet(q, 'header'),
        question: safeGet(q, 'question'),
        multiSelect: safeGet(q, 'multi_select') === true,
        options,
      };
    });
    return {
      type: 'question-asked',
      source: 'ask_user_question',
      callId,
      sessionId: sid,
      questionCount: items.length,
      optionCount: items.reduce((n, q) => n + q.options.length, 0),
      title: items.map((q) => q.header || q.question || q.id || '').filter(Boolean).join(' / ').slice(0, 120),
      questions: items,        // 结构留给后续用；桌宠现在只拿它切状态
    };
  };
  const clearQuestion = (callId, reason) => {
    const held = pendingQuestions.get(callId);
    if (!held) return;
    pendingQuestions.delete(callId);
    broadcast({ type: 'question-cleared', callId, reason, sessionId: held.sessionId });
    plog(`question cleared callId=${callId} reason=${reason} pending=${pendingQuestions.size}`);
  };
  const clearApproval = (id, outcome) => {
    const held = pendingApprovals.get(id);
    if (!held) return;
    pendingApprovals.delete(id);
    broadcast({ type: 'approval-decided', id, outcome: String(outcome), sessionId: held.sessionId });
    plog(`approval cleared id=${id} outcome=${String(outcome)} pending=${pendingApprovals.size}`);
  };

  const offs = [];

  // 监听器绝不能把异常抛回 DSH：cordis 的 listener 抛错会冒泡进触发方
  // （实测会变成“新建会话失败: cannot get property ... without inject”）。
  const guard = (name, fn) => (...args) => {
    try {
      fn(...args);
    } catch (e) {
      plog(`${name} listener error (已吞掉，不影响 DSH): ${e && e.message}`);
    }
  };

  // 3.1 实时 token：payload = { agent, frame }；frame = { type:start|chunk|end, attemptId, turn, step, index, chunk }
  offs.push(sub('agent/assistant-stream', guard('agent/assistant-stream', (payload) => {
    const frame = safeGet(payload, 'frame');
    if (!frame) return;
    sample('assistant-stream', payload);
    const sid = sessionIdOf(safeGet(payload, 'agent'));
    const ft = safeGet(frame, 'type');
    if (ft === 'start') {
      broadcast({ type: 'status', status: 'running', source: 'assistant-stream/start', sessionId: sid });
    } else if (ft === 'end') {
      // attempt 结束不等于整轮结束，但至少说明模型这一段不再吐字
      broadcast({ type: 'stream-end', sessionId: sid });
    } else if (ft === 'chunk') {
      const c = safeGet(frame, 'chunk');
      const ct = safeGet(c, 'type');
      if (ct === 'text-delta' || ct === 'reasoning-delta') {
        state.tokens++;
        if (config.debug && state.tokens % 20 === 1) plog(`token#${state.tokens} chunkKeys=${keysOf(c)}`);
        const text = safeGet(c, 'text');
        broadcast({ type: 'token', textLen: typeof text === 'string' ? text.length : 0, kind: ct, sessionId: sid });
      }
    }
  })));

  // 3.2 耐久会话事件：session/event(session, event)，用于“这轮真的结束了”
  offs.push(sub('session/event', guard('session/event', (a, b) => {
    const event = b || safeGet(a, 'event');
    const session = safeGet(a, 'session') || a;
    const t = safeGet(event, 'type');
    if (typeof t !== 'string') return;
    const sid = sessionIdOf(session);
    sample('session/event', { session: sid, eventType: t });
    if (t === 'turn/start') {
      broadcast({ type: 'status', status: 'running', source: 'turn/start', sessionId: sid });
    } else if (t === 'turn/end') {
      broadcast({ type: 'done', source: 'turn/end', sessionId: sid });
    } else if (t === 'assistant/message') {
      broadcast({ type: 'message', sessionId: sid });
    } else if (t === 'tool/call') {
      // 只认 ask_user_question（工具参数里带结构化 options）；其余工具调用不广播
      const data = safeGet(event, 'data');
      const callId = safeGet(data, 'callId');
      if (safeGet(data, 'name') !== 'ask_user_question') return;
      if (typeof callId !== 'string' || !callId) return;
      const questions = parseQuestionArgs(safeGet(data, 'arguments'));
      pendingQuestions.set(callId, { sessionId: sid });
      const payload = askPayload(callId, sid, questions);
      plog(`tool/call name=ask_user_question callId=${callId} questions=${payload.questionCount} options=${payload.optionCount}`);
      broadcast(payload);
    } else if (t === 'tool/result') {
      broadcast({ type: 'tool-result', sessionId: sid });
      // 同一 callId 的结果到达 → 问题已结算（答题或工具失败），撤掉待决策
      const callId = safeGet(safeGet(safeGet(event, 'data'), 'message'), 'toolCallId');
      if (typeof callId === 'string') clearQuestion(callId, 'tool-result');
    } else if (t === 'approval/asked') {
      // 审批弹窗：data = { id, toolName, callId?, reason? }
      // 与 ask_user_question 的区别：这里没有 questions[]/options[]，是“允许/拒绝”。
      const data = safeGet(event, 'data');
      const id = safeGet(data, 'id');
      if (typeof id !== 'string' || !id) return;
      const toolName = String(safeGet(data, 'toolName') || '');
      const rawReason = safeGet(data, 'reason');
      const callId = safeGet(data, 'callId');
      pendingApprovals.set(id, { sessionId: sid });
      const payload = {
        type: 'approval-asked',
        source: 'approval',
        id,
        toolName,
        callId: typeof callId === 'string' ? callId : null,
        reason: String(rawReason || '').slice(0, 120),
        title: (toolName ? `审批：${toolName}` : '审批待确认').slice(0, 120),
        sessionId: sid,
      };
      plog(`approval/asked id=${id} tool=${toolName} pending=${pendingApprovals.size}`);
      broadcast(payload);
    } else if (t === 'approval/decided') {
      // 审批已决（allow/deny）→ 撤掉对应的待决策提醒；不代答、不接管
      const data = safeGet(event, 'data');
      const id = safeGet(data, 'id');
      const outcome = safeGet(data, 'outcome');
      if (typeof id === 'string') clearApproval(id, outcome);
    }
  })));

  // 3.3 兼容旧版 agent/status（存在就用，不存在也只是少了这个来源）
  offs.push(sub('agent/status', guard('agent/status', (payload) => {
    sample('agent/status', payload);
    const status = safeGet(payload, 'status');
    if (typeof status === 'string') {
      broadcast({ type: 'status', status, source: 'agent/status', sessionId: sessionIdOf(safeGet(payload, 'agent')) });
    }
  })));

  offs.push(sub('agent/error', guard('agent/error', (payload) => {
    sample('agent/error', payload);
    const err = safeGet(payload, 'error');
    const message = safeGet(err, 'message');
    broadcast({ type: 'error', message: String(message || err || 'unknown'), sessionId: sessionIdOf(safeGet(payload, 'agent')) });
  })));

  offs.push(sub('agent/created', guard('agent/created', (payload) => {
    sample('agent/created', payload);
    broadcast({ type: 'agent', event: 'created', sessionId: sessionIdOf(safeGet(payload, 'agent')) });
  })));

  offs.push(sub('agent/disposed', guard('agent/disposed', (payload) => {
    sample('agent/disposed', payload);
    broadcast({ type: 'agent', event: 'disposed', sessionId: sessionIdOf(safeGet(payload, 'agent')) });
  })));

  try {
    // 同理：这里必须 return 注销函数，否则 apply 阶段就把订阅全部注销了
    ctx.effect(() => () => {
      for (const off of offs) {
        try {
          off();
        } catch {
          /* ignore */
        }
      }
      plog('subscriptions disposed');
    });
  } catch {
    /* ctx.effect 不可用也不影响监听 */
  }
}
