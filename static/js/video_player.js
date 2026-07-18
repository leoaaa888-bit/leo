/**
 * Low-latency H.264 playback via WebCodecs VideoDecoder + Canvas.
 */
class H264CanvasPlayer {
    constructor(canvas) {
        this.canvas = canvas;
        this.ctx = canvas.getContext('2d', { alpha: false, desynchronized: true });
        this.decoder = null;
        this.configured = false;
        this.waitingForKey = true;
        this.frameIndex = 0;
        this.width = 0;
        this.height = 0;
        this.onFramePresented = null;
        this._lastSps = null;
        this._lastPps = null;
        this._reconfiguring = false;
        this._lastReconfigureAt = 0;
        this._resizeObserver = null;
        this._staleFrameMs = 150;
        this._maxDecodeQueue = 3;
        this._lastDecodedPts = null;
        this._lastDecodeSentAt = 0;
        // Server and client are different machines, so their wall clocks differ
        // by an unknown amount. Never compare their timestamps directly: track
        // the smallest observed (clientNow - serverSentAt) as a baseline that
        // absorbs clock skew + best-case transit, and measure each frame's
        // lateness relative to it. Re-baselined periodically so clock drift
        // cannot wedge it.
        this._minServerOffset = null;
        this._minServerOffsetAt = 0;
        this._bindDisplayResize();
    }

    static OFFSET_REBASE_MS = 10000;

    /** Frame lateness in ms, immune to server/client clock skew. */
    _frameBacklogMs(serverSentAt) {
        const now = Date.now();
        const offset = now - serverSentAt;
        if (this._minServerOffset === null
            || offset < this._minServerOffset
            || now - this._minServerOffsetAt > H264CanvasPlayer.OFFSET_REBASE_MS) {
            this._minServerOffset = offset;
            this._minServerOffsetAt = now;
        }
        return offset - this._minServerOffset;
    }

    _bindDisplayResize() {
        const slot = this.canvas && this.canvas.parentElement;
        if (!slot || typeof ResizeObserver === 'undefined') {
            return;
        }
        this._resizeObserver = new ResizeObserver(() => {
            this._syncDisplaySize();
        });
        this._resizeObserver.observe(slot);
        this._syncDisplaySize();
    }

    /** Canvas bitmap size != CSS size; setting .width resets layout unless we re-apply. */
    _syncDisplaySize() {
        const canvas = this.canvas;
        if (!canvas) {
            return;
        }
        canvas.style.width = '100%';
        canvas.style.height = '100%';
        canvas.style.display = 'block';
    }

    _setBitmapSize(w, h) {
        if (!w || !h) {
            return;
        }
        if (this.canvas.width !== w || this.canvas.height !== h) {
            this.canvas.width = w;
            this.canvas.height = h;
            this.width = w;
            this.height = h;
        }
        this._syncDisplaySize();
    }

    static isSupported() {
        return typeof VideoDecoder !== 'undefined'
            && typeof EncodedVideoChunk !== 'undefined'
            && typeof VideoFrame !== 'undefined';
    }

    /** Find next Annex-B start code; returns {start, scLen} or null. */
    static findStartCode(data, from) {
        for (let i = from; i + 3 < data.length; i++) {
            if (data[i] === 0 && data[i + 1] === 0) {
                if (data[i + 2] === 1) {
                    return { start: i, scLen: 3 };
                }
                if (i + 3 < data.length && data[i + 2] === 0 && data[i + 3] === 1) {
                    return { start: i, scLen: 4 };
                }
            }
        }
        return null;
    }

    /** Split buffer into raw NAL payloads (no start codes / length prefixes). */
    static splitNalPayloads(nalu) {
        if (!nalu || nalu.length < 4) {
            return [];
        }

        const first = H264CanvasPlayer.findStartCode(nalu, 0);
        if (first && first.start === 0) {
            const payloads = [];
            let sc = first;
            while (sc) {
                const nalStart = sc.start + sc.scLen;
                const next = H264CanvasPlayer.findStartCode(nalu, nalStart);
                const nalEnd = next ? next.start : nalu.length;
                if (nalEnd > nalStart) {
                    payloads.push(nalu.subarray(nalStart, nalEnd));
                }
                sc = next;
            }
            return payloads;
        }

        // Single AVCC unit: 4-byte length + payload matching buffer size
        const len = ((nalu[0] << 24) | (nalu[1] << 16) | (nalu[2] << 8) | nalu[3]) >>> 0;
        if (len > 0 && len + 4 === nalu.length) {
            return [nalu.subarray(4)];
        }

        // Multiple AVCC units concatenated
        const payloads = [];
        let offset = 0;
        while (offset + 4 <= nalu.length) {
            const n = ((nalu[offset] << 24) | (nalu[offset + 1] << 16)
                | (nalu[offset + 2] << 8) | nalu[offset + 3]) >>> 0;
            if (n <= 0 || offset + 4 + n > nalu.length) {
                break;
            }
            payloads.push(nalu.subarray(offset + 4, offset + 4 + n));
            offset += 4 + n;
        }
        if (payloads.length) {
            return payloads;
        }

        // Fallback: treat whole buffer as one RBSP (best-effort)
        return [nalu];
    }

    /** Pack one or more NAL payloads as AVCC (length-prefixed). */
    static toAvcc(nalu) {
        const payloads = H264CanvasPlayer.splitNalPayloads(nalu);
        if (!payloads.length) {
            return new Uint8Array(0);
        }
        let total = 0;
        for (const p of payloads) {
            total += 4 + p.length;
        }
        const out = new Uint8Array(total);
        let o = 0;
        for (const p of payloads) {
            out[o++] = (p.length >>> 24) & 0xff;
            out[o++] = (p.length >>> 16) & 0xff;
            out[o++] = (p.length >>> 8) & 0xff;
            out[o++] = p.length & 0xff;
            out.set(p, o);
            o += p.length;
        }
        return out;
    }

    static rbsp(nalu) {
        const payloads = H264CanvasPlayer.splitNalPayloads(nalu);
        return payloads.length ? payloads[0] : new Uint8Array(0);
    }

    /** True if access unit contains an IDR slice. */
    static isKeyFrame(nalu) {
        const payloads = H264CanvasPlayer.splitNalPayloads(nalu);
        for (const p of payloads) {
            if (p.length && (p[0] & 0x1f) === 5) {
                return true;
            }
        }
        return false;
    }

    /** Primary VCL / useful type for logging; prefers IDR/non-IDR over AUD/SEI. */
    static primaryNaluType(nalu) {
        const payloads = H264CanvasPlayer.splitNalPayloads(nalu);
        let fallback = -1;
        for (const p of payloads) {
            if (!p.length) {
                continue;
            }
            const t = p[0] & 0x1f;
            if (t === 5 || t === 1) {
                return t;
            }
            if (fallback < 0) {
                fallback = t;
            }
        }
        return fallback;
    }

    static buildAvcC(spsNalu, ppsNalu) {
        const spsList = H264CanvasPlayer.splitNalPayloads(spsNalu);
        const ppsList = H264CanvasPlayer.splitNalPayloads(ppsNalu);
        const sps = spsList.find((p) => p.length && (p[0] & 0x1f) === 7) || spsList[0];
        const pps = ppsList.find((p) => p.length && (p[0] & 0x1f) === 8) || ppsList[0];
        if (!sps || sps.length < 4 || !pps || pps.length < 1) {
            return null;
        }
        const out = new Uint8Array(11 + sps.length + pps.length);
        let o = 0;
        out[o++] = 1;
        out[o++] = sps[1];
        out[o++] = sps[2];
        out[o++] = sps[3];
        out[o++] = 0xff;
        out[o++] = 0xe1;
        out[o++] = (sps.length >> 8) & 0xff;
        out[o++] = sps.length & 0xff;
        out.set(sps, o);
        o += sps.length;
        out[o++] = 1;
        out[o++] = (pps.length >> 8) & 0xff;
        out[o++] = pps.length & 0xff;
        out.set(pps, o);
        return { description: out, profile: sps[1], compat: sps[2], level: sps[3] };
    }

    configure(sps, pps) {
        if (!H264CanvasPlayer.isSupported()) {
            return false;
        }
        const avc = H264CanvasPlayer.buildAvcC(sps, pps);
        if (!avc) {
            console.error('invalid SPS/PPS for avcC');
            return false;
        }

        if (this.decoder) {
            try { this.decoder.close(); } catch (e) {}
            this.decoder = null;
        }

        this._lastSps = sps;
        this._lastPps = pps;
        this.waitingForKey = true;
        this.frameIndex = 0;
        this.configured = false;

        const codec = 'avc1.'
            + avc.profile.toString(16).padStart(2, '0')
            + avc.compat.toString(16).padStart(2, '0')
            + avc.level.toString(16).padStart(2, '0');

        try {
            this.decoder = new VideoDecoder({
                output: (frame) => this._onFrame(frame),
                error: (err) => {
                    console.error('VideoDecoder error:', err);
                    this.waitingForKey = true;
                    this.configured = false;
                    // Recreate decoder from last SPS/PPS so we are not permanently wedged.
                    const now = Date.now();
                    if (!this._reconfiguring
                        && this._lastSps && this._lastPps
                        && now - this._lastReconfigureAt > 1500) {
                        this._reconfiguring = true;
                        this._lastReconfigureAt = now;
                        try {
                            this.configure(this._lastSps, this._lastPps);
                        } catch (e) {
                            console.error('VideoDecoder reconfigure failed:', e);
                        } finally {
                            this._reconfiguring = false;
                        }
                    }
                },
            });
            this.decoder.configure({
                codec,
                description: avc.description,
                optimizeForLatency: true,
            });
            this.configured = true;
            console.log('WebCodecs VideoDecoder configured', codec);
            this._syncDisplaySize();
            return true;
        } catch (err) {
            console.error('VideoDecoder configure failed:', err);
            return false;
        }
    }

    _onFrame(frame) {
        try {
            const w = frame.displayWidth || frame.codedWidth;
            const h = frame.displayHeight || frame.codedHeight;
            this._setBitmapSize(w, h);
            this.ctx.drawImage(frame, 0, 0, this.canvas.width, this.canvas.height);
            if (typeof this.onFramePresented === 'function') {
                try { this.onFramePresented(this._lastDecodeSentAt); } catch (e) {}
            }
        } catch (err) {
            console.error('canvas draw error:', err);
        } finally {
            frame.close();
        }
    }

    static sanitizeTimestamp(pts, frameIndex, stepUs = 40000) {
        const maxTs = 9007199254740991;
        let t = pts;
        if (t == null || !Number.isFinite(t) || t < 0 || t > maxTs) {
            t = frameIndex * stepUs;
        } else {
            t = Math.floor(t);
        }
        return t;
    }

    decodeNalu(nalu, pts, serverSentAt = 0) {
        if (!this.configured || !this.decoder || this.decoder.state !== 'configured') {
            return;
        }

        const type = H264CanvasPlayer.primaryNaluType(nalu);
        // Skip pure parameter-set packets (already in description)
        if (type === 7 || type === 8) {
            return;
        }

        const isKey = H264CanvasPlayer.isKeyFrame(nalu);
        const staleMs = this._staleFrameMs || 150;
        const maxQueue = this._maxDecodeQueue || 3;

        if (serverSentAt > 0) {
            const backlogMs = this._frameBacklogMs(serverSentAt);
            if (!isKey && backlogMs > staleMs) {
                return;
            }
        }

        if (pts != null && this._lastDecodedPts != null && pts < this._lastDecodedPts && !isKey) {
            return;
        }

        const queueSize = this.decoder.decodeQueueSize;
        if (queueSize > maxQueue) {
            this.waitingForKey = true;
            if (!isKey) {
                return;
            }
        }

        if (this.waitingForKey && !isKey) {
            return;
        }
        if (isKey) {
            this.waitingForKey = false;
        }

        // Only feed VCL NALUs (+ optional SEI); drop AUD (9)
        const payloads = H264CanvasPlayer.splitNalPayloads(nalu).filter((p) => {
            if (!p.length) {
                return false;
            }
            const t = p[0] & 0x1f;
            return t !== 9 && t !== 7 && t !== 8;
        });
        if (!payloads.length) {
            return;
        }

        let total = 0;
        for (const p of payloads) {
            total += 4 + p.length;
        }
        const avcc = new Uint8Array(total);
        let o = 0;
        for (const p of payloads) {
            avcc[o++] = (p.length >>> 24) & 0xff;
            avcc[o++] = (p.length >>> 16) & 0xff;
            avcc[o++] = (p.length >>> 8) & 0xff;
            avcc[o++] = p.length & 0xff;
            avcc.set(p, o);
            o += p.length;
        }

        try {
            const timestamp = H264CanvasPlayer.sanitizeTimestamp(pts, this.frameIndex);
            const chunk = new EncodedVideoChunk({
                type: isKey ? 'key' : 'delta',
                timestamp: timestamp,
                data: avcc,
            });
            this.frameIndex++;
            this._lastDecodeSentAt = serverSentAt || 0;
            if (pts != null) {
                this._lastDecodedPts = pts;
            }
            this.decoder.decode(chunk);
        } catch (err) {
            console.error('decode failed:', err);
            this.waitingForKey = true;
        }
    }

    destroy() {
        if (this._resizeObserver) {
            this._resizeObserver.disconnect();
            this._resizeObserver = null;
        }
        if (this.decoder) {
            try { this.decoder.close(); } catch (e) {}
            this.decoder = null;
        }
        this.configured = false;
        this._lastDecodedPts = null;
        this._lastDecodeSentAt = 0;
        this._minServerOffset = null;
        this._minServerOffsetAt = 0;
    }
}
