/**
 * Pull-based PCM playback for Safari/iOS WebAudio streaming.
 */
class ScrcpyPcmProcessor extends AudioWorkletProcessor {
    constructor(options) {
        super();
        const opts = (options && options.processorOptions) || {};
        this.channels = Math.max(1, opts.channels || 2);
        this.chunks = [];
        this.chunkOffset = 0;
        this.queuedSamples = 0;

        this.port.onmessage = (event) => {
            const msg = event.data || {};
            if (msg.type === 'reset') {
                this.chunks = [];
                this.chunkOffset = 0;
                this.queuedSamples = 0;
                return;
            }
            if (msg.type === 'pcm' && msg.data && msg.data.length) {
                this.chunks.push(msg.data);
                this.queuedSamples += msg.data.length;
                while (this.chunks.length > 96) {
                    const dropped = this.chunks.shift();
                    if (dropped) {
                        this.queuedSamples = Math.max(0, this.queuedSamples - dropped.length);
                    }
                }
            }
        };
    }

    _pullSample() {
        while (this.chunks.length > 0) {
            const chunk = this.chunks[0];
            if (this.chunkOffset < chunk.length) {
                return chunk[this.chunkOffset++];
            }
            this.chunks.shift();
            this.chunkOffset = 0;
        }
        return 0;
    }

    process(inputs, outputs) {
        const output = outputs[0];
        if (!output || !output.length) {
            return true;
        }
        const frames = output[0].length;
        for (let i = 0; i < frames; i++) {
            for (let ch = 0; ch < output.length; ch++) {
                output[ch][i] = this._pullSample();
            }
        }
        if (this.queuedSamples > 0) {
            this.queuedSamples = Math.max(0, this.queuedSamples - frames * output.length);
        }
        return true;
    }
}

registerProcessor('scrcpy-pcm', ScrcpyPcmProcessor);
