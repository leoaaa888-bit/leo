class AudioParser {
    constructor(onAudioCallback, debug = false) {
        this.debug = debug;
        this.onAudioCallback = onAudioCallback;
        this.reset();
    }

    reset() {
        this.buffer = new Uint8Array(0);
        this.codecId = null;
        this.audioConfig = null;
        this.packetCount = 0;
        this._capacity = 0;
        this._storage = null;
    }

    appendData(data) {
        if (!data || !data.length) {
            return;
        }
        const oldLen = this.buffer.length;
        const needed = oldLen + data.length;
        if (!this._storage || needed > this._capacity) {
            let cap = Math.max(this._capacity * 2, 64 * 1024);
            while (cap < needed) {
                cap *= 2;
            }
            const grown = new Uint8Array(cap);
            if (oldLen) {
                grown.set(this.buffer, 0);
            }
            this._storage = grown;
            this._capacity = cap;
        }
        this._storage.set(data, oldLen);
        this.buffer = this._storage.subarray(0, needed);
        this.scrcpyProcessBuffer();
    }

    dataView(byteOffset = 0, byteLength = this.buffer.length - byteOffset) {
        return new DataView(this.buffer.buffer, this.buffer.byteOffset + byteOffset, byteLength);
    }

    static isAdtsFrame(data) {
        return data.length >= 7 && data[0] === 0xff && (data[1] & 0xf0) === 0xf0;
    }

    static extractAudioSpecificConfig(payload) {
        if (!payload || payload.length < 2) {
            return payload;
        }

        if (payload.length === 2) {
            return payload;
        }

        // Android MediaCodec CSD section: 8-byte id + 8-byte size + data
        if (payload.length >= 16) {
            const view = new DataView(payload.buffer, payload.byteOffset, payload.byteLength);
            let size = 0;
            try {
                size = Number(view.getBigUint64(8, false));
            } catch (e) {
                size = view.getUint32(12, false);
            }
            if (size > 0 && 16 + size <= payload.length) {
                return payload.slice(16, 16 + size);
            }
        }

        // Length-prefixed ASC: 00 00 00 02 12 10
        if (payload.length >= 6 && payload[0] === 0 && payload[1] === 0 && payload[2] === 0) {
            const ascLen = ((payload[0] << 24) | (payload[1] << 16) | (payload[2] << 8) | payload[3]) >>> 0;
            if (ascLen > 0 && 4 + ascLen <= payload.length) {
                return payload.slice(4, 4 + ascLen);
            }
        }

        return payload.slice(0, 2);
    }

    static parseAudioSpecificConfig(config) {
        const asc = AudioParser.extractAudioSpecificConfig(config);
        const audioObjectType = (asc[0] >> 3) & 0x1f;
        const samplingFrequencyIndex = ((asc[0] & 0x07) << 1) | ((asc[1] >> 7) & 0x01);
        const channelConfiguration = (asc[1] >> 3) & 0x0f;
        return { audioObjectType, samplingFrequencyIndex, channelConfiguration, asc };
    }

    static wrapAdts(aacFrame, audioConfig) {
        if (AudioParser.isAdtsFrame(aacFrame)) {
            return aacFrame;
        }

        const { audioObjectType, samplingFrequencyIndex, channelConfiguration } =
            AudioParser.parseAudioSpecificConfig(audioConfig);
        const frameLength = aacFrame.length + 7;
        const adts = new Uint8Array(frameLength);
        adts[0] = 0xff;
        adts[1] = 0xf1;
        adts[2] = ((audioObjectType - 1) << 6) | (samplingFrequencyIndex << 2) | (channelConfiguration >> 2);
        adts[3] = ((channelConfiguration & 0x03) << 6) | (frameLength >> 11);
        adts[4] = (frameLength >> 3) & 0xff;
        adts[5] = ((frameLength & 0x07) << 5) | 0x1f;
        adts[6] = 0xfc;
        adts.set(aacFrame, 7);
        return adts;
    }

    static isConfigPacket(flagsOrPtsMsb) {
        // scrcpy: CONFIG flag is bit 63 of big-endian PTS → MSB of first header byte.
        return (flagsOrPtsMsb & 0x80) !== 0;
    }

    static looksLikeAsc(payload) {
        if (!payload || payload.length < 2 || payload.length > 8) {
            return false;
        }
        const objectType = (payload[0] >> 3) & 0x1f;
        const freqIdx = ((payload[0] & 0x07) << 1) | ((payload[1] >> 7) & 0x01);
        const channels = (payload[1] >> 3) & 0x0f;
        return objectType >= 1 && objectType <= 5 && freqIdx <= 12 && channels >= 1 && channels <= 8;
    }

    static parsePacketHeader(view, byteOffset = 0) {
        const size = view.getUint32(byteOffset + 8, false);
        if (!view.getBigInt64) {
            return { pts: null, isConfig: false, size, flags: 0 };
        }
        const raw = view.getBigInt64(byteOffset, false);
        const isConfig = (raw & 0x8000000000000000n) !== 0n;
        const masked = raw & 0x7FFFFFFFFFFFFFFFn;
        let pts = null;
        if (!isConfig && masked > 0n && masked <= 9007199254740991n) {
            pts = Number(masked);
            if (!Number.isFinite(pts)) {
                pts = null;
            }
        }
        return { pts, isConfig, size, flags: view.getUint8(byteOffset) };
    }

    scrcpyProcessBuffer() {
        let startIndex = 0;

        if (this.codecId == null) {
            if (this.buffer.length >= 4) {
                const view = this.dataView(0, 4);
                this.codecId = view.getUint32(0, false);
                console.log('audio codec:', '0x' + this.codecId.toString(16));
                // 0 = disabled by device, 1 = configuration error
                if (this.codecId === 0 || this.codecId === 1) {
                    console.warn('audio stream disabled by device, codecId=', this.codecId);
                    if (this.onAudioCallback) {
                        this.onAudioCallback({
                            type: 'disabled',
                            data: { codecId: this.codecId },
                        });
                    }
                } else if (this.onAudioCallback) {
                    this.onAudioCallback({
                        type: 'codec',
                        data: { codecId: this.codecId },
                    });
                }
                startIndex = 4;
            }
        } else if (this.codecId === 0 || this.codecId === 1) {
            // Stream disabled — drain nothing useful.
            this.buffer = (this._storage || this.buffer).subarray(0, 0);
            return;
        } else {
            while (this.buffer.length - startIndex > 12) {
                const view = this.dataView(startIndex, 12);
                const { pts, isConfig, size, flags } = AudioParser.parsePacketHeader(view, 0);
                if (size <= 0 || size > 1024 * 1024) {
                    startIndex += 1;
                    continue;
                }
                if (this.buffer.length - startIndex >= 12 + size) {
                    const payload = this.buffer.subarray(startIndex + 12, startIndex + 12 + size);
                    let packetIsConfig = isConfig || AudioParser.isConfigPacket(flags);
                    // Fallback: tiny ASC-looking payload before any config arrived.
                    if (!packetIsConfig && !this.audioConfig && AudioParser.looksLikeAsc(payload)) {
                        packetIsConfig = true;
                    }
                    this.packetCount++;

                    if (this.debug && this.packetCount <= 8) {
                        console.log('audio packet', this.packetCount,
                            'flags=0x' + flags.toString(16),
                            'size=' + size,
                            'config=' + packetIsConfig);
                    }

                    if (packetIsConfig) {
                        // Copy config — it must outlive the ring buffer compact.
                        this.audioConfig = payload.slice();
                        if (this.onAudioCallback) {
                            this.onAudioCallback({
                                type: 'config',
                                data: this.audioConfig,
                            });
                        }
                    } else if (this.audioConfig) {
                        if (this.onAudioCallback) {
                            this.onAudioCallback({
                                type: 'frame',
                                data: payload,
                                pts: pts,
                            });
                        }
                    } else if (this.packetCount <= 8) {
                        console.warn('audio frame before config, waiting… size=' + size);
                    }
                    startIndex += 12 + size;
                } else {
                    break;
                }
            }
        }

        if (startIndex > 0) {
            if (startIndex >= this.buffer.length) {
                this.buffer = (this._storage || this.buffer).subarray(0, 0);
            } else {
                const storage = this._storage || this.buffer;
                const remain = this.buffer.length - startIndex;
                storage.copyWithin(0, startIndex, startIndex + remain);
                this.buffer = storage.subarray(0, remain);
            }
            this._maybeShrink();
        }
    }

    _maybeShrink() {
        const minCap = 64 * 1024;
        if (this._capacity > minCap && this.buffer.length < this._capacity / 4) {
            const newCap = Math.max(minCap, this._capacity >>> 1);
            const shrunk = new Uint8Array(newCap);
            if (this.buffer.length) {
                shrunk.set(this.buffer, 0);
            }
            this._storage = shrunk;
            this._capacity = newCap;
            this.buffer = shrunk.subarray(0, this.buffer.length);
        }
    }
}
