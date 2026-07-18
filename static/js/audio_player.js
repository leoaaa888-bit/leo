/**
 * AAC playback via WebCodecs AudioDecoder + AudioContext.
 * Safari: AudioWorklet on HTTPS, buffer scheduling on HTTP (Worklet needs secure context).
 */
class AacWebAudioPlayer {
    static WORKLET_URL = '/static/js/audio_pcm_worklet.js?v=3';

    static _isIOS() {
        const ua = navigator.userAgent || '';
        return /iPhone|iPad|iPod/i.test(ua)
            || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
    }

    static _isSafari() {
        const ua = navigator.userAgent || '';
        return (/Safari/i.test(ua) || AacWebAudioPlayer._isIOS())
            && !/Chrome|Chromium|CriOS|FxiOS|EdgiOS/i.test(ua);
    }

    static _preferWorklet() {
        return AacWebAudioPlayer._isSafari()
            && typeof window !== 'undefined'
            && !!window.isSecureContext;
    }

    static MIN_BUFFER_AHEAD_S = AacWebAudioPlayer._isSafari() ? 0.08 : 0.08;
    static MAX_BUFFER_AHEAD_S = AacWebAudioPlayer._isSafari() ? 0.35 : 0.4;
    static CATCHUP_AHEAD_S = AacWebAudioPlayer._isSafari() ? 0.12 : 0.14;
    static MAX_ACTIVE_SOURCES = AacWebAudioPlayer._isSafari() ? 2 : 10;
    static MIN_SCHEDULE_SAMPLES = AacWebAudioPlayer._isSafari() ? 512 : 1024;
    static MAX_DECODE_QUEUE = 96;
    static MAX_PREUNLOCK_CHUNKS = 48;

    constructor() {
        this.decoder = null;
        this.audioContext = null;
        this.configured = false;
        this.frameIndex = 0;
        this.unlocked = false;
        this._gestureUnlocked = false;
        this._pendingAsc = null;
        this._sampleRate = 48000;
        this._channels = 2;
        this._codec = 'mp4a.40.2';
        this.onError = null;
        this._decodedFrames = 0;
        this._scheduledChunks = 0;
        this._playbackMode = AacWebAudioPlayer._preferWorklet() ? 'worklet' : 'buffer';

        this._gainNode = null;
        this._workletNode = null;
        this._workletReady = false;
        this._workletLoading = null;
        this._pendingPcm = [];
        this._preUnlockChunks = [];

        this._nextPlayTime = 0;
        this._activeSources = new Set();
        this._ring = null;
        this._ringCap = 0;
        this._ringRPos = 0;
        this._ringWPos = 0;
        this._ringCount = 0;
        this._pumping = false;
        this._keeperElement = null;
        this._keeperSource = null;
        this._keeperGain = null;
    }

    static isSupported() {
        return typeof AudioDecoder !== 'undefined'
            && typeof EncodedAudioChunk !== 'undefined'
            && (typeof AudioContext !== 'undefined' || typeof webkitAudioContext !== 'undefined');
    }

    static sampleRateFromAsc(asc) {
        const idx = ((asc[0] & 0x07) << 1) | ((asc[1] >> 7) & 0x01);
        const rates = [96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000, 7350];
        return rates[idx] || 48000;
    }

    static channelsFromAsc(asc) {
        return (asc[1] >> 3) & 0x0f || 2;
    }

    static stripAdts(frameData) {
        if (!frameData || frameData.length < 7) {
            return frameData;
        }
        if (frameData[0] !== 0xff || (frameData[1] & 0xf0) !== 0xf0) {
            return frameData;
        }
        const headerLen = (frameData[1] & 0x01) ? 7 : 9;
        if (frameData.length <= headerLen) {
            return frameData;
        }
        return frameData.subarray(headerLen);
    }

    static _resampleChannel(src, srcRate, dstRate) {
        if (!src || !src.length || srcRate === dstRate || !srcRate || !dstRate) {
            return src;
        }
        const ratio = srcRate / dstRate;
        const outLen = Math.max(1, Math.round(src.length / ratio));
        const out = new Float32Array(outLen);
        const last = src[src.length - 1] || 0;
        for (let i = 0; i < outLen; i++) {
            const pos = i * ratio;
            const idx = Math.floor(pos);
            const frac = pos - idx;
            const a = idx < src.length ? src[idx] : last;
            const b = idx + 1 < src.length ? src[idx + 1] : a;
            out[i] = a + (b - a) * frac;
        }
        return out;
    }

    _playbackRate() {
        return (this.audioContext && this.audioContext.sampleRate) || this._sampleRate;
    }

    isUnlocked() {
        return this._gestureUnlocked;
    }

    _shouldUseWorklet() {
        return this._playbackMode === 'worklet' && this._workletReady;
    }

    setKeeperElement(element) {
        this._keeperElement = element || null;
    }

    _setAudioSessionPlayback() {
        try {
            if (navigator.audioSession && navigator.audioSession.type !== 'ambient') {
                navigator.audioSession.type = 'ambient';
            }
        } catch (e) {}
    }

    _playKeeperElement() {
        const el = this._keeperElement;
        if (!el) {
            return;
        }
        try {
            const silent = 'data:audio/wav;base64,UklGRigAAABXQVZFZm10IBAAAAABAAEARKwAAIhYAQACABAAAABkYXRhAgAAAAEA';
            if (!el.src || el.src.indexOf('data:audio/wav') !== 0) {
                el.src = silent;
            }
            el.loop = true;
            el.muted = false;
            el.volume = 0.001;
            const p = el.play();
            if (p && typeof p.catch === 'function') {
                p.catch((e) => console.warn('audio keeper play:', e));
            }
        } catch (e) {
            console.warn('audio keeper failed:', e);
        }
    }

    _createContext() {
        if (this.audioContext) {
            return this.audioContext;
        }
        const Ctx = window.AudioContext || window.webkitAudioContext;
        if (!Ctx) {
            return null;
        }
        try {
            if (AacWebAudioPlayer._isSafari()) {
                this.audioContext = new Ctx();
            } else {
                this.audioContext = new Ctx({
                    sampleRate: this._sampleRate,
                    latencyHint: 'playback',
                });
            }
        } catch (err) {
            try {
                this.audioContext = new Ctx();
            } catch (err2) {
                console.warn('AudioContext create failed:', err2);
                return null;
            }
        }
        return this.audioContext;
    }

    _ensureOutputChain() {
        if (!this.audioContext) {
            return false;
        }
        if (!this._gainNode) {
            this._gainNode = this.audioContext.createGain();
            this._gainNode.gain.value = 1.0;
            this._gainNode.connect(this.audioContext.destination);
        }
        return true;
    }

    _bindKeeperElement(ctx) {
        if (!this._keeperElement || !ctx) {
            return;
        }
        if (!this._keeperSource) {
            try {
                this._ensureOutputChain();
                this._keeperGain = ctx.createGain();
                this._keeperGain.gain.value = 0;
                this._keeperSource = ctx.createMediaElementSource(this._keeperElement);
                this._keeperSource.connect(this._keeperGain);
                this._keeperGain.connect(this._gainNode);
            } catch (err) {
                console.warn('audio keeper bind failed:', err);
            }
        }
        this._playKeeperElement();
    }

    _fallbackToBuffer(reason) {
        if (this._playbackMode === 'buffer') {
            return;
        }
        console.warn('audio playback fallback to buffer:', reason);
        this._playbackMode = 'buffer';
        if (this._workletNode) {
            try { this._workletNode.disconnect(); } catch (e) {}
            this._workletNode = null;
        }
        this._workletReady = false;
        this._workletLoading = null;
        if (!this._ring) {
            this._resetRing();
        }
        if (this._pendingPcm.length) {
            const pending = this._pendingPcm;
            this._pendingPcm = [];
            for (const interleaved of pending) {
                const ch = Math.max(1, this._channels);
                const frames = Math.floor(interleaved.length / ch);
                if (!frames) {
                    continue;
                }
                const chData = [];
                for (let c = 0; c < ch; c++) {
                    const channel = new Float32Array(frames);
                    for (let i = 0; i < frames; i++) {
                        channel[i] = interleaved[i * ch + c];
                    }
                    chData.push(channel);
                }
                this._writeRing(chData, frames);
            }
        }
        this._pumpPlayback();
    }

    async _ensureWorklet() {
        if (this._playbackMode !== 'worklet') {
            return false;
        }
        const ctx = this.audioContext;
        if (!ctx) {
            return false;
        }
        if (this._workletReady && this._workletNode) {
            return true;
        }
        if (this._workletLoading) {
            return this._workletLoading;
        }
        this._workletLoading = (async () => {
            try {
                await ctx.audioWorklet.addModule(AacWebAudioPlayer.WORKLET_URL);
                if (this._workletNode) {
                    try { this._workletNode.disconnect(); } catch (e) {}
                }
                this._workletNode = new AudioWorkletNode(ctx, 'scrcpy-pcm', {
                    numberOfInputs: 0,
                    numberOfOutputs: 1,
                    outputChannelCount: [Math.max(1, this._channels)],
                    processorOptions: { channels: Math.max(1, this._channels) },
                });
                this._ensureOutputChain();
                this._workletNode.connect(this._gainNode);
                this._workletReady = true;
                this._flushPendingPcm();
                console.log('audio worklet ready, channels=', this._channels);
                return true;
            } catch (err) {
                this._fallbackToBuffer(err && err.message ? err.message : err);
                return false;
            } finally {
                this._workletLoading = null;
            }
        })();
        return this._workletLoading;
    }

    _resetWorkletQueue() {
        this._pendingPcm = [];
        if (this._workletNode) {
            try {
                this._workletNode.port.postMessage({ type: 'reset' });
            } catch (e) {}
        }
    }

    _flushPendingPcm() {
        if (!this._workletNode || !this._pendingPcm.length) {
            return;
        }
        const pending = this._pendingPcm;
        this._pendingPcm = [];
        for (const chunk of pending) {
            this._sendPcm(chunk, true);
        }
    }

    _sendPcm(interleaved, fromFlush) {
        if (!interleaved || !interleaved.length) {
            return;
        }
        if (this._shouldUseWorklet()) {
            try {
                const copy = new Float32Array(interleaved);
                this._workletNode.port.postMessage({ type: 'pcm', data: copy }, [copy.buffer]);
            } catch (e) {
                this._workletNode.port.postMessage({ type: 'pcm', data: new Float32Array(interleaved) });
            }
            this._scheduledChunks++;
            return;
        }
        if (!fromFlush) {
            this._pendingPcm.push(interleaved);
            if (this._pendingPcm.length > 64) {
                this._pendingPcm.shift();
            }
        }
    }

    _resetRing() {
        const ch = Math.max(1, this._channels);
        const rate = this._playbackRate();
        this._ringCap = Math.max(8192, Math.ceil(rate * 0.5));
        this._ring = [];
        for (let i = 0; i < ch; i++) {
            this._ring.push(new Float32Array(this._ringCap));
        }
        this._ringRPos = 0;
        this._ringWPos = 0;
        this._ringCount = 0;
        this._nextPlayTime = 0;
        this._pumping = false;
    }

    _stopScheduledSources() {
        for (const source of this._activeSources) {
            try {
                source.onended = null;
                source.stop(0);
            } catch (e) {}
            try {
                source.disconnect();
            } catch (e) {}
        }
        this._activeSources.clear();
        this._nextPlayTime = 0;
        this._pumping = false;
    }

    _playSilentClick(ctx) {
        try {
            this._ensureOutputChain();
            const buffer = ctx.createBuffer(1, 1, ctx.sampleRate || 44100);
            const source = ctx.createBufferSource();
            source.buffer = buffer;
            source.connect(this._gainNode || ctx.destination);
            source.start(0);
        } catch (err) {}
    }

    unlockSync() {
        this._setAudioSessionPlayback();
        const ctx = this._createContext();
        if (!ctx) {
            return false;
        }
        this._gestureUnlocked = true;
        this._playSilentClick(ctx);
        this._bindKeeperElement(ctx);
        if (typeof ctx.resume === 'function') {
            try {
                ctx.resume();
            } catch (e) {}
        }
        this._ensureOutputChain();
        this.unlocked = ctx.state === 'running';
        if (this._pendingAsc && !this.configured) {
            this._configureDecoder(this._pendingAsc);
        }
        this._flushPreUnlockChunks();
        this._pumpPlayback();
        return true;
    }

    unlock() {
        this.unlockSync();
        const ctx = this.audioContext;
        if (!ctx) {
            return Promise.resolve(false);
        }
        const finish = async () => {
            if (this._playbackMode === 'worklet') {
                await this._ensureWorklet();
            }
            if (ctx.state === 'suspended' && typeof ctx.resume === 'function') {
                try {
                    await ctx.resume();
                } catch (e) {}
            }
            this.unlocked = ctx.state === 'running';
            if (this.unlocked) {
                this._ensureOutputChain();
                if (this._pendingAsc && !this.configured) {
                    this._configureDecoder(this._pendingAsc);
                }
                this._flushPreUnlockChunks();
                this._pumpPlayback();
            }
            console.log('audio unlock done: mode=', this._playbackMode,
                'worklet=', this._workletReady, 'ctx=', ctx.state,
                'secure=', !!window.isSecureContext);
            return this.unlocked;
        };
        if (ctx.state === 'running') {
            return finish();
        }
        if (typeof ctx.resume !== 'function') {
            return finish();
        }
        return ctx.resume().then(() => finish()).catch(() => finish());
    }

    async resume() {
        if (!this.audioContext) {
            return false;
        }
        if (this.audioContext.state === 'suspended' || this.audioContext.state === 'interrupted') {
            try {
                await this.audioContext.resume();
            } catch (err) {
                console.warn('AudioContext resume failed:', err);
                return false;
            }
        }
        if (this._playbackMode === 'worklet') {
            await this._ensureWorklet();
        }
        this._playKeeperElement();
        this.unlocked = this.audioContext.state === 'running';
        if (this.unlocked) {
            this._pumpPlayback();
        }
        return this.unlocked;
    }

    softReset() {
        if (this.decoder) {
            try { this.decoder.close(); } catch (e) {}
            this.decoder = null;
        }
        this._stopScheduledSources();
        this._resetWorkletQueue();
        this._preUnlockChunks = [];
        this._pendingPcm = [];
        this._ring = null;
        this._ringCount = 0;
        this._playbackMode = AacWebAudioPlayer._preferWorklet() ? 'worklet' : 'buffer';
        this.configured = false;
        this.frameIndex = 0;
        this._decodedFrames = 0;
        this._scheduledChunks = 0;
    }

    _configureDecoder(asc) {
        if (!AacWebAudioPlayer.isSupported() || !asc || asc.length < 2) {
            return false;
        }

        const newRate = AacWebAudioPlayer.sampleRateFromAsc(asc);
        this._channels = AacWebAudioPlayer.channelsFromAsc(asc);
        const objectType = (asc[0] >> 3) & 0x1f;
        this._codec = 'mp4a.40.' + objectType;
        this._pendingAsc = asc;
        this._sampleRate = newRate;

        if (!this.audioContext) {
            this._createContext();
        } else if (!AacWebAudioPlayer._isSafari()
            && this.audioContext.sampleRate !== newRate) {
            try { this.audioContext.close(); } catch (e) {}
            this.audioContext = null;
            this._gainNode = null;
            this._createContext();
            if (this._gestureUnlocked && this.audioContext && this.audioContext.state === 'suspended') {
                this.audioContext.resume().catch(() => {});
            }
        }

        this._stopScheduledSources();
        this._resetWorkletQueue();
        this._resetRing();

        if (this.decoder) {
            try { this.decoder.close(); } catch (e) {}
            this.decoder = null;
        }
        if (this._workletNode) {
            try { this._workletNode.disconnect(); } catch (e) {}
            this._workletNode = null;
            this._workletReady = false;
            this._workletLoading = null;
        }

        this._ensureOutputChain();

        const playbackRate = this._playbackRate();
        console.log('WebAudio AAC config:', Array.from(asc).map((b) => b.toString(16).padStart(2, '0')).join(' '),
            'decodeRate=', this._sampleRate, 'playbackRate=', playbackRate,
            'ch=', this._channels, 'codec=', this._codec,
            'mode=', this._playbackMode, 'secure=', !!window.isSecureContext,
            'gestureUnlocked=', this._gestureUnlocked,
            'ctxState=', this.audioContext ? this.audioContext.state : 'none');

        const config = {
            codec: this._codec,
            sampleRate: this._sampleRate,
            numberOfChannels: this._channels,
            description: asc,
        };

        if (typeof AudioDecoder !== 'undefined' && typeof AudioDecoder.isConfigSupported === 'function') {
            AudioDecoder.isConfigSupported(config).then((supported) => {
                console.log('AudioDecoder.isConfigSupported(', config.codec, ')=', supported);
            }).catch(() => {});
        }

        try {
            this.decoder = new AudioDecoder({
                output: (frame) => this._onDecodedFrame(frame),
                error: (err) => {
                    console.error('AudioDecoder error:', err);
                    this.configured = false;
                    if (typeof this.onError === 'function') {
                        try { this.onError(err); } catch (e) {}
                    }
                },
            });
            this.decoder.configure(config);
            this.configured = true;
            this.frameIndex = 0;
            if (this._gestureUnlocked) {
                if (this._playbackMode === 'worklet') {
                    this._ensureWorklet();
                } else {
                    this._pumpPlayback();
                }
            }
            return true;
        } catch (err) {
            console.error('AudioDecoder configure failed:', err);
            this.configured = false;
            if (typeof this.onError === 'function') {
                try { this.onError(err); } catch (e) {}
            }
            return false;
        }
    }

    configure(configPayload) {
        if (!AacWebAudioPlayer.isSupported()) {
            return false;
        }
        const asc = AudioParser.extractAudioSpecificConfig(configPayload);
        if (!asc || asc.length < 2) {
            console.error('invalid AAC config', configPayload);
            return false;
        }
        return this._configureDecoder(asc);
    }

    _interleaveChannels(chData, frames, channels) {
        const ch = Math.max(1, channels);
        const inCh = Math.max(1, chData.length);
        const out = new Float32Array(frames * ch);
        for (let i = 0; i < frames; i++) {
            for (let c = 0; c < ch; c++) {
                const src = chData[Math.min(c, inCh - 1)];
                out[i * ch + c] = src ? src[i] : 0;
            }
        }
        return out;
    }

    _storePreUnlock(chData, fframes) {
        const copy = chData.map((ch) => new Float32Array(ch));
        this._preUnlockChunks.push({ chData: copy, fframes: fframes });
        if (this._preUnlockChunks.length > AacWebAudioPlayer.MAX_PREUNLOCK_CHUNKS) {
            this._preUnlockChunks.shift();
        }
    }

    _flushPreUnlockChunks() {
        if (!this._preUnlockChunks.length) {
            return;
        }
        const chunks = this._preUnlockChunks;
        this._preUnlockChunks = [];
        for (const item of chunks) {
            this._playChunk(item.chData, item.fframes);
        }
    }

    _writeRing(chData, fframes) {
        const ring = this._ring;
        if (!ring) {
            return;
        }
        const ringCh = ring.length;
        const cap = this._ringCap;
        let count = this._ringCount;
        let wpos = this._ringWPos;

        if (count + fframes > cap) {
            const drop = count + fframes - cap;
            this._ringRPos = (this._ringRPos + drop) % cap;
            count = cap - fframes;
        }

        for (let ch = 0; ch < ringCh; ch++) {
            const dst = ring[ch];
            const src = ch < chData.length ? chData[ch] : chData[0];
            if (wpos + fframes <= cap) {
                dst.set(src, wpos);
            } else {
                const first = cap - wpos;
                dst.set(src.subarray(0, first), wpos);
                dst.set(src.subarray(first), 0);
            }
        }

        this._ringWPos = (wpos + fframes) % cap;
        this._ringCount = count + fframes;
    }

    _syncScheduleClock() {
        const ctx = this.audioContext;
        if (!ctx) {
            return;
        }
        const now = ctx.currentTime;
        if (!this._nextPlayTime || this._nextPlayTime < now) {
            this._nextPlayTime = now + AacWebAudioPlayer.MIN_BUFFER_AHEAD_S;
        } else if (this._nextPlayTime - now > AacWebAudioPlayer.MAX_BUFFER_AHEAD_S) {
            this._nextPlayTime = now + AacWebAudioPlayer.CATCHUP_AHEAD_S;
        }
    }

    _pumpPlayback() {
        if (this._pumping || this._shouldUseWorklet() || !this._gestureUnlocked) {
            return;
        }
        const ctx = this.audioContext;
        if (!ctx || !this._ring || !this._ensureOutputChain()) {
            return;
        }
        if (ctx.state !== 'running') {
            if (typeof ctx.resume === 'function') {
                ctx.resume().then(() => this._pumpPlayback()).catch(() => {});
            }
            return;
        }

        this._pumping = true;
        try {
            while (this._activeSources.size < AacWebAudioPlayer.MAX_ACTIVE_SOURCES
                && this._ringCount >= AacWebAudioPlayer.MIN_SCHEDULE_SAMPLES) {
                if (!this._scheduleOneChunk()) {
                    break;
                }
            }
        } finally {
            this._pumping = false;
        }
    }

    _scheduleOneChunk() {
        const ctx = this.audioContext;
        if (!ctx || !this._ring) {
            return false;
        }

        const ring = this._ring;
        const ringCh = ring.length;
        const cap = this._ringCap;
        const playbackRate = this._playbackRate();
        const toRead = Math.min(
            this._ringCount,
            AacWebAudioPlayer._isSafari() ? 4096 : 1024
        );
        const rpos = this._ringRPos;

        const buffer = ctx.createBuffer(ringCh, toRead, playbackRate);
        for (let ch = 0; ch < ringCh; ch++) {
            const dst = buffer.getChannelData(ch);
            const src = ring[ch];
            if (rpos + toRead <= cap) {
                dst.set(src.subarray(rpos, rpos + toRead));
            } else {
                const first = cap - rpos;
                dst.set(src.subarray(rpos, cap));
                dst.set(src.subarray(0, toRead - first), first);
            }
        }

        this._ringRPos = (rpos + toRead) % cap;
        this._ringCount -= toRead;

        this._syncScheduleClock();
        const startAt = Math.max(ctx.currentTime + 0.002, this._nextPlayTime);
        const source = ctx.createBufferSource();
        source.buffer = buffer;
        source.connect(this._gainNode);
        try {
            source.start(startAt);
        } catch (err) {
            try {
                const retryAt = ctx.currentTime + 0.002;
                source.start(retryAt);
                this._nextPlayTime = retryAt + (toRead / playbackRate);
            } catch (err2) {
                return false;
            }
            this._activeSources.add(source);
            this._scheduledChunks++;
            const self = this;
            source.onended = function () {
                self._activeSources.delete(source);
                try { source.disconnect(); } catch (e) {}
                self._pumpPlayback();
            };
            return true;
        }

        this._nextPlayTime = startAt + (toRead / playbackRate);
        this._activeSources.add(source);
        this._scheduledChunks++;
        const self = this;
        source.onended = function () {
            self._activeSources.delete(source);
            try { source.disconnect(); } catch (e) {}
            self._pumpPlayback();
        };
        return true;
    }

    _schedulePerFrame(chData, fframes) {
        const ctx = this.audioContext;
        if (!ctx || !this._ensureOutputChain()) {
            if (!this._ring) {
                this._resetRing();
            }
            this._writeRing(chData, fframes);
            this._pumpPlayback();
            return;
        }
        if (ctx.state !== 'running') {
            if (!this._ring) {
                this._resetRing();
            }
            this._writeRing(chData, fframes);
            if (typeof ctx.resume === 'function') {
                ctx.resume().then(() => this._pumpPlayback()).catch(() => {});
            }
            return;
        }
        if (this._activeSources.size >= AacWebAudioPlayer.MAX_ACTIVE_SOURCES) {
            if (!this._ring) {
                this._resetRing();
            }
            this._writeRing(chData, fframes);
            this._pumpPlayback();
            return;
        }

        const ringCh = chData.length;
        const playbackRate = this._playbackRate();
        const buffer = ctx.createBuffer(ringCh, fframes, playbackRate);
        for (let ch = 0; ch < ringCh; ch++) {
            buffer.getChannelData(ch).set(chData[ch]);
        }

        this._syncScheduleClock();
        const startAt = Math.max(ctx.currentTime + 0.002, this._nextPlayTime);
        const source = ctx.createBufferSource();
        source.buffer = buffer;
        source.connect(this._gainNode);
        try {
            source.start(startAt);
        } catch (err) {
            if (!this._ring) {
                this._resetRing();
            }
            this._writeRing(chData, fframes);
            this._pumpPlayback();
            return;
        }

        this._nextPlayTime = startAt + (fframes / playbackRate);
        this._activeSources.add(source);
        this._scheduledChunks++;
        const self = this;
        source.onended = function () {
            self._activeSources.delete(source);
            try { source.disconnect(); } catch (e) {}
            self._pumpPlayback();
        };
    }

    _playChunk(chData, fframes) {
        if (this._shouldUseWorklet()) {
            const interleaved = this._interleaveChannels(chData, fframes, Math.max(chData.length, this._channels));
            this._sendPcm(interleaved);
            if (!this._workletReady) {
                this._ensureWorklet();
            }
            return;
        }
        if (!AacWebAudioPlayer._isSafari()) {
            this._schedulePerFrame(chData, fframes);
            return;
        }
        if (!this._ring) {
            this._resetRing();
        }
        this._writeRing(chData, fframes);
        this._pumpPlayback();
    }

    _onDecodedFrame(frame) {
        try {
            if (!this.audioContext) {
                this._createContext();
            }
            if (!this.audioContext) {
                return;
            }

            const fch = frame.numberOfChannels;
            const fframes = frame.numberOfFrames;
            if (!fframes) {
                return;
            }

            this._decodedFrames++;
            if (this._decodedFrames === 1 || this._decodedFrames % 200 === 0) {
                console.log('WebAudio decoded frames:', this._decodedFrames,
                    'mode=', this._playbackMode,
                    'worklet=', this._workletReady,
                    'ring=', this._ringCount,
                    'ctx=', this.audioContext.state,
                    'gesture=', this._gestureUnlocked,
                    'scheduled=', this._scheduledChunks);
            }

            const frameRate = frame.sampleRate || this._sampleRate;
            const playbackRate = this._playbackRate();
            const chData = [];
            for (let ch = 0; ch < fch; ch++) {
                const data = new Float32Array(fframes);
                try {
                    frame.copyTo(data, { planeIndex: ch, format: 'f32-planar' });
                } catch (e) {
                    frame.copyTo(data, { planeIndex: ch });
                }
                chData.push(
                    frameRate === playbackRate
                        ? data
                        : AacWebAudioPlayer._resampleChannel(data, frameRate, playbackRate)
                );
            }

            if (!this._gestureUnlocked) {
                this._storePreUnlock(chData, fframes);
                return;
            }

            this._playChunk(chData, fframes);
        } catch (err) {
            console.error('audio playback error:', err);
        } finally {
            frame.close();
        }
    }

    static sanitizeTimestamp(pts, frameIndex, stepUs) {
        const maxTs = 9007199254740991;
        let t = pts;
        if (t == null || !Number.isFinite(t) || t < 0 || t > maxTs) {
            t = frameIndex * stepUs;
        } else {
            t = Math.floor(t);
        }
        return t;
    }

    decodeFrame(frameData, pts) {
        if (!this.configured || !this.decoder || this.decoder.state !== 'configured') {
            return;
        }
        if (this.decoder.decodeQueueSize > AacWebAudioPlayer.MAX_DECODE_QUEUE) {
            return;
        }

        try {
            const raw = AacWebAudioPlayer.stripAdts(frameData);
            const frameDurationUs = Math.round(1024 / this._sampleRate * 1000000);
            const timestamp = AacWebAudioPlayer.sanitizeTimestamp(pts, this.frameIndex, frameDurationUs);
            this.frameIndex++;
            const chunk = new EncodedAudioChunk({
                type: 'key',
                timestamp: timestamp,
                duration: frameDurationUs,
                data: raw,
            });
            this.decoder.decode(chunk);
        } catch (err) {
            console.warn('audio decode failed:', err);
        }
    }

    destroy() {
        if (this.decoder) {
            try { this.decoder.close(); } catch (e) {}
            this.decoder = null;
        }
        this._stopScheduledSources();
        if (this._workletNode) {
            try { this._workletNode.disconnect(); } catch (e) {}
            this._workletNode = null;
        }
        this._workletReady = false;
        this._workletLoading = null;
        this._pendingPcm = [];
        this._preUnlockChunks = [];
        if (this._keeperSource) {
            try { this._keeperSource.disconnect(); } catch (e) {}
            this._keeperSource = null;
        }
        if (this._keeperGain) {
            try { this._keeperGain.disconnect(); } catch (e) {}
            this._keeperGain = null;
        }
        if (this._gainNode) {
            try { this._gainNode.disconnect(); } catch (e) {}
            this._gainNode = null;
        }
        if (this.audioContext) {
            this.audioContext.close().catch(() => {});
            this.audioContext = null;
        }
        this._ring = null;
        this._ringCount = 0;
        this.configured = false;
        this.unlocked = false;
        this._gestureUnlocked = false;
        this._pendingAsc = null;
        this._decodedFrames = 0;
        this._scheduledChunks = 0;
        this._keeperElement = null;
    }
}
