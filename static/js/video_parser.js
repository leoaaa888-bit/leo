class VideoParser {
    constructor(onNaluCallback, debug = false) {
        this.debug = debug
        this.onNaluCallback = onNaluCallback;
        this.reset();
    }

    reset() {
        this.buffer = new Uint8Array(0);
        this.name = null;
        this.width = null;
        this.height = null;
        this.sps = null;
        this.pps = null;
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
            let cap = Math.max(this._capacity * 2, 256 * 1024);
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

    static parsePacketHeader(view, byteOffset = 0) {
        const size = view.getUint32(byteOffset + 8, false);
        if (!view.getBigInt64) {
            return { pts: null, isConfig: false, size };
        }
        const raw = view.getBigInt64(byteOffset, false);
        const isConfig = (raw & 0x8000000000000000n) !== 0n;
        const masked = raw & 0x7FFFFFFFFFFFFFFFn;
        let pts = null;
        // Keep PTS within Number.MAX_SAFE_INTEGER so WebCodecs int64 is never exceeded.
        if (!isConfig && masked > 0n && masked <= 9007199254740991n) {
            pts = Number(masked);
            if (!Number.isFinite(pts)) {
                pts = null;
            }
        }
        return { pts, isConfig, size };
    }

    scrcpyProcessBuffer() {
        let startIndex = 0;
        if (this.name == null) {
            if (this.buffer.length < 64) {
                return;
            }
            const name = this.buffer.slice(0, 64);
            this.name = new TextDecoder().decode(name).replace(/\0.*$/, '');
            console.log("Device name:" + this.name);
            if (this.onNaluCallback) {
                this.onNaluCallback({
                    type: 'name',
                    data: { "name": this.name }
                });
            }
            startIndex = 64;
        }
        if (this.width == null) {
            if (this.buffer.length - startIndex < 12) {
                if (startIndex > 0) {
                    this._compactBuffer(startIndex);
                }
                return;
            }
            const view = this.dataView(startIndex, 12);
            const codecId = view.getUint32(0, false);
            this.width = view.getUint32(4, false);
            this.height = view.getUint32(8, false);
            console.log("codec:" + codecId.toString(16) + " width:" + this.width + " height:" + this.height);
            if (this.onNaluCallback) {
                this.onNaluCallback({
                    type: 'screen_size',
                    data: { "width": this.width, "height": this.height }
                });
            }
            startIndex += 12;
        }
        while (this.buffer.length - startIndex > 12) {
            const view = this.dataView(startIndex, 12);
            const { pts, size } = VideoParser.parsePacketHeader(view, 0);
            if (size <= 0 || size > 10 * 1024 * 1024) {
                startIndex += 1;
                continue;
            }
            if (this.buffer.length - startIndex >= 12 + size) {
                const nalu = this.buffer.subarray(startIndex + 12, startIndex + 12 + size);
                this.processBuffer(nalu, pts);
                startIndex += 12 + size;
            } else {
                break;
            }
        }
        this._compactBuffer(startIndex);
    }

    _compactBuffer(startIndex) {
        if (startIndex <= 0) {
            return;
        }
        if (startIndex >= this.buffer.length) {
            this.buffer = (this._storage || this.buffer).subarray(0, 0);
            this._maybeShrink();
            return;
        }
        const storage = this._storage || this.buffer;
        const remain = this.buffer.length - startIndex;
        storage.copyWithin(0, startIndex, startIndex + remain);
        this.buffer = storage.subarray(0, remain);
        this._maybeShrink();
    }

    _maybeShrink() {
        const minCap = 256 * 1024;
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

    readAvccLength(nalu) {
        if (nalu.length < 4) {
            return -1;
        }
        return ((nalu[0] << 24) | (nalu[1] << 16) | (nalu[2] << 8) | nalu[3]) >>> 0;
    }

    isAvccNalu(nalu) {
        if (nalu.length < 5) {
            return false;
        }
        if (nalu[0] === 0 && nalu[1] === 0 && (nalu[2] === 1 || (nalu[2] === 0 && nalu[3] === 1))) {
            return false;
        }
        const len = this.readAvccLength(nalu);
        return len > 0 && len + 4 === nalu.length;
    }

    naluType(nalu) {
        if (this.isAvccNalu(nalu)) {
            return nalu[4] & 0x1f;
        }
        if (nalu.length >= 4 && nalu[0] === 0 && nalu[1] === 0 && nalu[2] === 1) {
            return nalu[3] & 0x1f;
        }
        if (nalu.length >= 5 && nalu[0] === 0 && nalu[1] === 0 && nalu[2] === 0 && nalu[3] === 1) {
            return nalu[4] & 0x1f;
        }
        return -1;
    }

    spsPayload(sps) {
        if (this.isAvccNalu(sps)) {
            return sps.slice(4);
        }
        if (sps.length >= 4 && sps[0] === 0 && sps[1] === 0 && sps[2] === 1) {
            return sps.slice(3);
        }
        if (sps.length >= 5 && sps[0] === 0 && sps[1] === 0 && sps[2] === 0 && sps[3] === 1) {
            return sps.slice(4);
        }
        return sps;
    }

    findSequence(arr, sequence, startIndex = 0) {
        const seqLength = sequence.length;
        for (let i = startIndex; i <= arr.length - seqLength; i++) {
            let match = true;
            for (let j = 0; j < seqLength; j++) {
                if (arr[i + j] !== sequence[j]) {
                    match = false;
                    break;
                }
            }
            if (match) {
                return i;
            }
        }
        return -1;
    }

    processBuffer(nalu, pts) {
        if (!nalu || nalu.length < 5) {
            return;
        }

        const nalu_type = this.naluType(nalu);
        if (nalu_type === 1) {
            if (this.debug) {
                console.log("P frame", nalu.length);
            }
        } else if (nalu_type === 5) {
            if (this.debug) {
                console.log("I frame", nalu.length);
            }
            // 生命周期排查用：网络层面是否真的收到过关键帧，跟"收到了但解码器
            // 没吐出画面"是两码事，两者要分得清才知道卡在传输还是卡在解码。
            if (this.onNaluCallback) {
                this.onNaluCallback({ type: 'idr-seen', data: { size: nalu.length } });
            }
        } else if (nalu_type === 7) {
            if (this.isAvccNalu(nalu)) {
                this.sps = nalu;
            } else {
                const next_pos = this.findSequence(nalu, [0, 0, 0, 1], 5);
                if (next_pos > 0) {
                    this.sps = nalu.slice(0, next_pos);
                    this.processBuffer(nalu.slice(next_pos), pts);
                } else {
                    this.sps = nalu;
                }
            }
            const sps = this.sps;
            if (sps && sps.length > 4) {
                if (this.onNaluCallback) {
                    this.onNaluCallback({ type: 'sps-seen', data: { size: sps.length } });
                }
                let ret = SPSParser.parseSPS(this.spsPayload(sps));
                if (this.onNaluCallback) {
                    this.onNaluCallback({
                        type: 'size_change',
                        data: { "width": ret.present_size.width, "height": ret.present_size.height }
                    });
                }
            }
            return;
        } else if (nalu_type === 8) {
            if (this.isAvccNalu(nalu)) {
                this.pps = nalu;
            } else {
                const next_pos = this.findSequence(nalu, [0, 0, 0, 1], 5);
                if (next_pos > 0) {
                    this.pps = nalu.slice(0, next_pos);
                    this.processBuffer(nalu.slice(next_pos), pts);
                } else {
                    this.pps = nalu;
                }
            }
            if (this.pps && this.pps.length > 4 && this.onNaluCallback) {
                this.onNaluCallback({ type: 'pps-seen', data: { size: this.pps.length } });
            }
            return;
        } else if (this.debug) {
            console.log("unknown frame type", nalu[0], nalu[1], nalu[2], nalu[3], nalu_type);
        }

        if (this.pps != null && this.sps != null) {
            if (this.onNaluCallback) {
                this.onNaluCallback({
                    type: 'init',
                    data: { "pps": this.pps, "sps": this.sps }
                });
            }
            this.pps = null;
            this.sps = null;
        }

        if (this.onNaluCallback) {
            this.onNaluCallback({
                type: 'nalu',
                data: nalu,
                pts: pts
            });
        }
    }
}
