import { afterEach, beforeEach, describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const voiceSource = await readFile(
  new URL('../../../server/static/modules/voice.js', import.meta.url),
  'utf8',
);

const playbackStub = `
class VoicePlayback {
  constructor() {
    this.isPlaying = false;
    this.queueLength = 0;
    this.aecActive = true;
    this.rms = 0;
  }
  destroy() {}
  stop() { this.isPlaying = false; this.queueLength = 0; }
  pause() {}
  resume() {}
  enqueue() {}
  setVolume() {}
}`;

const vadStub = `
class VoiceVAD {
  constructor(options) { this.options = options; }
  async start() { return globalThis.__vadStartResult ?? true; }
  stop() {}
  destroy() {}
}`;

const micStub = `
async function acquireVoiceStream() {
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: { exact: 'all' } },
    });
  } catch (err) {
    if (err?.name !== 'OverconstrainedError') throw err;
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true },
    });
    return { stream, aecAll: false };
  }
  if (stream.getAudioTracks()[0]?.getSettings().echoCancellation === 'all') {
    return { stream, aecAll: true };
  }
  stream.getTracks().forEach((track) => track.stop());
  stream = await navigator.mediaDevices.getUserMedia({
    audio: { echoCancellation: true },
  });
  return { stream, aecAll: false };
}`;

const moduleSource = voiceSource
  .replace("import { VoicePlayback } from './voice-playback.js';", playbackStub)
  .replace("import { VoiceVAD } from './voice-vad.js';", vadStub)
  .replace("import { acquireVoiceStream } from './voice-mic.js';", micStub)
  .replace("import { basePath } from '/shared/base-path.js';", "const basePath = '';")
  .replace("new URL('./voice-worklet.js', import.meta.url)", "'voice-worklet.js'");

const { VoiceManager } = await import(
  `data:text/javascript;base64,${Buffer.from(moduleSource).toString('base64')}`,
);

class FakeTrack {
  constructor(settings = {}) {
    this.settings = settings;
    this.stopped = false;
  }

  getSettings() {
    return this.settings;
  }

  stop() {
    this.stopped = true;
  }
}

class FakeStream {
  constructor(settings = {}) {
    this.track = new FakeTrack(settings);
  }

  get active() {
    return !this.track.stopped;
  }

  getTracks() {
    return [this.track];
  }

  getAudioTracks() {
    return [this.track];
  }
}

class FakeAudioContext {
  static instances = [];

  constructor() {
    this.sampleRate = 48000;
    this.closed = false;
    this.sources = [];
    this.audioWorklet = { addModule: async () => {} };
    FakeAudioContext.instances.push(this);
  }

  createMediaStreamSource(stream) {
    this.sources.push(stream);
    return { connect() {} };
  }

  createGain() {
    return { gain: { value: 1 }, connect() {} };
  }

  close() {
    this.closed = true;
  }
}

class FakeAudioWorkletNode {
  constructor() {
    this.port = { onmessage: null };
  }

  connect() {}
  disconnect() {}
}

const originalGlobals = new Map();
const managers = [];

function setGlobal(name, value) {
  if (!originalGlobals.has(name)) {
    originalGlobals.set(name, Object.getOwnPropertyDescriptor(globalThis, name));
  }
  Object.defineProperty(globalThis, name, {
    configurable: true,
    writable: true,
    value,
  });
}

function newManager() {
  const manager = new VoiceManager();
  manager._connected = true;
  manager._ws = {
    readyState: 1,
    sent: [],
    send(message) { this.sent.push(message); },
    close() {},
  };
  managers.push(manager);
  return manager;
}

beforeEach(() => {
  setGlobal('__vadStartResult', true);
  FakeAudioContext.instances = [];
  setGlobal('AudioContext', FakeAudioContext);
  setGlobal('AudioWorkletNode', FakeAudioWorkletNode);
  setGlobal('WebSocket', { OPEN: 1 });
  setGlobal('navigator', { mediaDevices: { getUserMedia: async () => new FakeStream() } });
});

afterEach(() => {
  for (const manager of managers.splice(0)) manager.disconnect();
  for (const [name, descriptor] of originalGlobals) {
    if (descriptor) Object.defineProperty(globalThis, name, descriptor);
    else delete globalThis[name];
  }
  originalGlobals.clear();
});

describe('VoiceManager native AEC', () => {
  it('accepts exact all only when getSettings confirms all', async () => {
    const stream = new FakeStream({ echoCancellation: 'all' });
    const calls = [];
    navigator.mediaDevices.getUserMedia = async (constraints) => {
      calls.push(constraints);
      return stream;
    };
    const manager = newManager();

    assert.equal(await manager._ensureMediaStream(), stream);
    assert.equal(manager._aecAll, true);
    assert.equal(calls.length, 1);
    assert.deepEqual(calls[0].audio.echoCancellation, { exact: 'all' });
  });

  it('falls back to boolean AEC for OverconstrainedError, but does not retry permission errors', async () => {
    const fallbackStream = new FakeStream({ echoCancellation: true });
    const calls = [];
    let attempt = 0;
    navigator.mediaDevices.getUserMedia = async (constraints) => {
      calls.push(constraints);
      attempt += 1;
      if (attempt === 1) {
        const error = new Error('all unsupported');
        error.name = 'OverconstrainedError';
        error.constraint = 'echoCancellation';
        throw error;
      }
      return fallbackStream;
    };
    const manager = newManager();

    assert.equal(await manager._ensureMediaStream(), fallbackStream);
    assert.equal(manager._aecAll, false);
    assert.equal(calls.length, 2);
    assert.equal(calls[1].audio.echoCancellation, true);

    const permissionManager = newManager();
    let permissionCalls = 0;
    const permissionError = new Error('permission denied');
    permissionError.name = 'NotAllowedError';
    navigator.mediaDevices.getUserMedia = async () => {
      permissionCalls += 1;
      throw permissionError;
    };
    await assert.rejects(permissionManager._ensureMediaStream(), { name: 'NotAllowedError' });
    assert.equal(permissionCalls, 1);
  });

  it('passes one cached stream to both VAD and AudioWorklet recording', async () => {
    const stream = new FakeStream({ echoCancellation: 'all' });
    navigator.mediaDevices.getUserMedia = async () => stream;
    const manager = newManager();

    manager._mode = 'vad';
    await manager._startVAD();
    assert.equal(await manager._vad.options.getStream(), stream);
    assert.equal(await manager._vad.options.resumeStream(), stream);
    await manager.startRecording();
    assert.equal(FakeAudioContext.instances.at(-1).sources[0], stream);
  });

  it('shares one pending microphone request between concurrent callers', async () => {
    const stream = new FakeStream({ echoCancellation: 'all' });
    let resolveRequest;
    let calls = 0;
    const pending = new Promise((resolve) => { resolveRequest = resolve; });
    navigator.mediaDevices.getUserMedia = async () => {
      calls += 1;
      await pending;
      return stream;
    };
    const manager = newManager();

    const first = manager._ensureMediaStream();
    const second = manager._ensureMediaStream();
    assert.equal(calls, 1);
    resolveRequest();
    assert.deepEqual(await Promise.all([first, second]), [stream, stream]);
  });

  it('keeps a pending AUTO stream alive when switching to PTT', async () => {
    const stream = new FakeStream({ echoCancellation: 'all' });
    let resolveRequest;
    let calls = 0;
    const pending = new Promise((resolve) => { resolveRequest = resolve; });
    navigator.mediaDevices.getUserMedia = async () => {
      calls += 1;
      await pending;
      return stream;
    };
    const manager = newManager();
    manager._mode = 'vad';

    const autoAcquisition = manager._ensureMediaStream();
    manager.setMode('ptt');
    const recording = manager.startRecording();
    resolveRequest();
    await Promise.all([autoAcquisition, recording]);

    assert.equal(calls, 1);
    assert.equal(stream.track.stopped, false);
    assert.strictEqual(FakeAudioContext.instances.at(-1).sources[0], stream);
  });

  it('stops an in-flight VAD resume when switching to PTT', async () => {
    const stream = new FakeStream({ echoCancellation: 'all' });
    let resolveRequest;
    const pending = new Promise((resolve) => { resolveRequest = resolve; });
    navigator.mediaDevices.getUserMedia = async () => {
      await pending;
      return stream;
    };
    const manager = newManager();
    manager._mode = 'vad';
    const existingVad = {
      running: false,
      start: async () => {
        await manager._ensureMediaStream();
        existingVad.running = true;
        return true;
      },
      stop() { this.running = false; },
      destroy() { this.running = false; },
    };
    manager._vad = existingVad;

    const resume = manager._startVAD();
    await new Promise((resolve) => setImmediate(resolve));
    manager.setMode('ptt');
    resolveRequest();
    await resume;

    assert.equal(existingVad.running, false);
    assert.equal(stream.track.stopped, true);
    assert.equal(manager._mediaStream, null);
  });

  it('holds TTS-time PCM until sustained speech starts a server-confirmed probe', async () => {
    const allStream = new FakeStream({ echoCancellation: 'all' });
    navigator.mediaDevices.getUserMedia = async () => allStream;
    const allManager = newManager();
    allManager._mode = 'vad';
    await allManager._startVAD();
    allManager._ttsPlaying = true;
    allManager._vad.options.onSpeechStart();
    await new Promise((resolve) => setImmediate(resolve));
    const pcm = new ArrayBuffer(4);
    allManager._workletNode.port.onmessage({ data: pcm });
    assert.deepEqual(allManager._ws.sent, []);
    assert.equal(allManager.isRecording, true);
    allManager._vad.options.onSpeechRealStart();
    assert.deepEqual(allManager._ws.sent, []);
    for (let frame = 0; frame < 19; frame++) {
      allManager._vad.options.onFrameProcessed({ isSpeech: 0.9 });
    }
    assert.deepEqual(allManager._ws.sent, [JSON.stringify({ type: 'barge_probe' }), pcm]);
    assert.equal(allManager._ttsPlaying, true);
    allManager._resolveBargeProbe(true);
    assert.equal(allManager._ttsPlaying, false);
    assert.equal(allManager._holdPcm, false);
  });

  it('keeps TTS half-duplex until playback AEC is active', async () => {
    const stream = new FakeStream({ echoCancellation: 'all' });
    navigator.mediaDevices.getUserMedia = async () => stream;
    const manager = newManager();
    manager._mode = 'vad';
    await manager._startVAD();
    manager._ttsPlaying = true;
    manager._playback.aecActive = false;
    manager._vad.options.onSpeechStart();
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(manager.isRecording, false);
    assert.deepEqual(manager._ws.sent, []);

    manager._playback.aecActive = true;
    manager._vad.options.onSpeechStart();
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(manager.isRecording, true);
  });

  it('discards TTS-time PCM on VAD misfire without interrupting', async () => {
    const stream = new FakeStream({ echoCancellation: 'all' });
    navigator.mediaDevices.getUserMedia = async () => stream;
    const manager = newManager();
    manager._mode = 'vad';
    await manager._startVAD();
    manager._ttsPlaying = true;
    manager._vad.options.onSpeechStart();
    await new Promise((resolve) => setImmediate(resolve));
    manager._workletNode.port.onmessage({ data: new ArrayBuffer(2) });
    manager._vad.options.onMisfire();
    assert.deepEqual(manager._ws.sent, [JSON.stringify({ type: 'discard_audio' })]);
    assert.equal(manager._heldPcm.length, 0);
    assert.equal(manager._holdPcm, false);
  });

  it('keeps fallback mode half-duplex', async () => {
    const fallbackStream = new FakeStream({ echoCancellation: true });
    navigator.mediaDevices.getUserMedia = async () => fallbackStream;
    const fallbackManager = newManager();
    fallbackManager._mode = 'vad';
    await fallbackManager._startVAD();
    fallbackManager._ttsPlaying = true;
    fallbackManager._vad.options.onSpeechStart();
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepEqual(fallbackManager._ws.sent, []);
    assert.equal(fallbackManager.isRecording, false);
  });

  it('releases the stream when VAD initialization returns false', async () => {
    const stream = new FakeStream({ echoCancellation: 'all' });
    navigator.mediaDevices.getUserMedia = async () => stream;
    setGlobal('__vadStartResult', false);
    const manager = newManager();
    manager._mode = 'vad';
    await manager._startVAD();

    assert.equal(manager._vad, null);
    assert.equal(manager._mediaStream, null);
    assert.equal(stream.track.stopped, true);
  });

  it('releases the microphone track when push-to-talk recording ends', async () => {
    const stream = new FakeStream({ echoCancellation: 'all' });
    navigator.mediaDevices.getUserMedia = async () => stream;
    const manager = newManager();

    await manager.startRecording();
    assert.equal(manager.isRecording, true);
    manager.stopRecording();
    assert.equal(stream.track.stopped, true);
    assert.equal(manager._mediaStream, null);
    assert.deepEqual(manager._ws.sent, [JSON.stringify({ type: 'speech_end' })]);
  });
});
