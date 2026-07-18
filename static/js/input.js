class ScrcpyInput {
    static INJECT_TEXT_MAX_LENGTH = 300;
    static TYPE_INJECT_TEXT = 1;
    static TYPE_SET_CLIPBOARD = 9;
    // scrcpy mouse pointer id (all bits set)
    static POINTER_ID_MOUSE = 0xFFFFFFFFFFFFFFFFn;
    static POINTER_ID_FINGER = 0xFFFFFFFFFFFFFFFEn;
    static BUTTON_PRIMARY = 1 << 0;

    static FUNCTION_KEY_CODES = new Set([
        'Enter', 'Backspace', 'Tab', 'Escape', 'CapsLock', 'NumLock', 'ScrollLock',
        'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight',
        'ShiftLeft', 'ShiftRight', 'ControlLeft', 'ControlRight',
        'AltLeft', 'AltRight', 'MetaLeft', 'MetaRight',
        'Delete', 'Insert', 'Home', 'End', 'PageUp', 'PageDown',
        'F1', 'F2', 'F3', 'F4', 'F5', 'F6', 'F7', 'F8', 'F9', 'F10', 'F11', 'F12',
        'ContextMenu', 'PrintScreen', 'Pause',
        'NumpadEnter', 'NumpadAdd', 'NumpadSubtract', 'NumpadMultiply', 'NumpadDivide',
        'NumpadDecimal',
    ]);

    constructor(callback, videoElement, width, height, options = {}) {
        this.callback = callback;
        this.videoElement = videoElement;
        this.width = width;
        this.height = height;
        this.debug = options.debug || false;
        this.onFocusChange = options.onFocusChange || null;
        this.onUserGesture = options.onUserGesture || null;

        this.wrapperElement = options.wrapperElement || videoElement.parentElement;
        this.keyboardInputElement = options.mobileInputElement || options.keyboardInputElement || null;
        // For canvas / non-video surfaces: use logical media size instead of videoWidth.
        this.getMediaSize = options.getMediaSize || null;

        this.pointerX = 0;
        this.pointerY = 0;
        this.isPointerDown = false;
        this.rightButtonIsPressed = false;
        this.activeTouchId = null;
        this.isTouchPrimary = options.isTouchPrimary ?? ScrcpyInput.isTouchPrimaryDevice();
        this.keyboardMappingEnabled = options.keyboardMappingEnabled ?? false;
        this.keyboardInputActive = false;
        this.keyboardInputLastValue = '';
        this.isComposing = false;
        this.compositionText = '';
        this.clipboardSequence = 0;
        this.hasFocusedOnce = false;
        this._pressedKeys = new Set();

        this.bindPointerEvents();
        if (!this.isTouchPrimary) {
            this.bindKeyboardInputEvents();
            this.bindDocumentKeyboardEvents();
        }
    }

    static isDesktopView() {
        return !ScrcpyInput.isTouchPrimaryDevice();
    }

    static isTouchPrimaryDevice() {
        const coarse = window.matchMedia('(pointer: coarse)').matches;
        const fine = window.matchMedia('(pointer: fine)').matches;
        return coarse && !fine;
    }

    static clamp(value, min, max) {
        return Math.min(max, Math.max(min, value));
    }

    /** Match scrcpy sc_float_to_i16fp for values in [-1, 1]. */
    static floatToI16fp(f) {
        const clamped = ScrcpyInput.clamp(f, -1, 1);
        let i = Math.round(clamped * 0x8000);
        if (i >= 0x7fff) {
            i = 0x7fff;
        }
        if (i < -0x8000) {
            i = -0x8000;
        }
        return i;
    }

    /** Match scrcpy scroll serialization: normalize wheel delta into [-16, 16] then to i16fp. */
    static wheelDeltaToScrollFp(delta) {
        // DOM deltaMode: 0=pixel, 1=line, 2=page
        // Convert roughly to "notches" then clamp to scrcpy's [-16, 16] range.
        let notches = -delta / 100;
        notches = ScrcpyInput.clamp(notches, -16, 16);
        return ScrcpyInput.floatToI16fp(notches / 16);
    }

    static utf8ByteLengthSafe(text, maxBytes) {
        const encoder = new TextEncoder();
        const bytes = encoder.encode(text);
        if (bytes.length <= maxBytes) {
            return bytes;
        }
        // Avoid splitting a multi-byte UTF-8 character.
        let end = maxBytes;
        while (end > 0 && (bytes[end] & 0xc0) === 0x80) {
            end--;
        }
        return bytes.subarray(0, end);
    }

    bindPointerEvents() {
        const video = this.videoElement;

        // On touch-primary devices, ignore synthetic mouse events after touch
        // to avoid duplicate DOWN (finger + mouse) injections.
        if (!this.isTouchPrimary) {
            video.addEventListener('mousedown', (event) => this.onPointerDown(event));
            video.addEventListener('mousemove', (event) => this.onPointerMove(event));
            video.addEventListener('mouseup', (event) => this.onPointerUp(event));
            video.addEventListener('mouseleave', (event) => {
                if (this.isPointerDown) {
                    this.onPointerUp(event);
                }
            });
        }

        video.addEventListener('touchstart', (event) => this.onTouchStart(event), { passive: false });
        video.addEventListener('touchmove', (event) => this.onTouchMove(event), { passive: false });
        video.addEventListener('touchend', (event) => this.onTouchEnd(event), { passive: false });
        video.addEventListener('touchcancel', (event) => this.onTouchEnd(event), { passive: false });

        video.addEventListener('contextmenu', (event) => {
            event.preventDefault();
        });

        video.addEventListener('wheel', (event) => {
            const pos = this.clientToDevice(event.clientX, event.clientY);
            if (!pos) {
                return;
            }
            let dx = event.deltaX;
            let dy = event.deltaY;
            if (event.deltaMode === 1) {
                dx *= 16;
                dy *= 16;
            } else if (event.deltaMode === 2) {
                dx *= 800;
                dy *= 800;
            }
            const data = this.createScrollProtocolData(
                pos.x, pos.y, this.width, this.height,
                dx, dy, event.buttons || 0
            );
            this.callback(data);
            event.preventDefault();
        }, { passive: false });

        const handleUserGesture = () => {
            if (this.onUserGesture) {
                this.onUserGesture();
            }
        };

        const focusForKeyboard = () => {
            if (this.keyboardMappingEnabled && !this.isTouchPrimary) {
                this.activateKeyboardInput();
            }
        };

        // pointerdown/touchstart keep AudioContext unlock inside the gesture stack (iOS).
        video.addEventListener('pointerdown', (event) => {
            handleUserGesture();
            focusForKeyboard();
        });
        video.addEventListener('touchstart', handleUserGesture, { passive: true });
        video.addEventListener('click', handleUserGesture);
        video.addEventListener('touchend', handleUserGesture, { passive: true });
        if (this.wrapperElement) {
            this.wrapperElement.addEventListener('pointerdown', (event) => {
                handleUserGesture();
                if (event.target === this.wrapperElement) {
                    focusForKeyboard();
                }
            });
        }

        // Backgrounding mid-drag often drops mouseup/touchend; clear stuck state.
        this._onVisibilityChange = () => {
            if (document.hidden) {
                this.releasePointerAndTouch();
                this.releasePressedKeys();
            }
        };
        document.addEventListener('visibilitychange', this._onVisibilityChange);
        window.addEventListener('pagehide', this._onVisibilityChange);
    }

    releasePointerAndTouch() {
        if (this.isPointerDown) {
            this.isPointerDown = false;
            try {
                this.sendTouch(1, 0, {
                    actionButton: ScrcpyInput.BUTTON_PRIMARY,
                    buttons: 0,
                });
            } catch (e) {}
        }
        if (this.activeTouchId != null) {
            this.activeTouchId = null;
            try {
                this.sendTouch(1, 0, {
                    pointerId: ScrcpyInput.POINTER_ID_FINGER,
                    buttons: 0,
                });
            } catch (e) {}
        }
        if (this.rightButtonIsPressed) {
            this.rightButtonIsPressed = false;
            try {
                this.sendKeyCode(null, 1, 4);
            } catch (e) {}
        }
    }

    bindKeyboardInputEvents() {
        if (!this.keyboardInputElement) {
            return;
        }

        const input = this.keyboardInputElement;

        input.addEventListener('compositionstart', () => {
            this.isComposing = true;
            this.compositionText = '';
        });

        input.addEventListener('compositionupdate', (event) => {
            this.compositionText = event.data || '';
        });

        input.addEventListener('compositionend', (event) => {
            this.isComposing = false;
            const committed = event.data || this.compositionText || '';
            this.compositionText = '';
            if (committed) {
                this.commitKeyboardText(committed);
                return;
            }
            requestAnimationFrame(() => this.syncKeyboardInputToDevice());
        });

        input.addEventListener('input', (event) => {
            if (!this.keyboardMappingEnabled || this.isComposing) {
                return;
            }
            if (event instanceof InputEvent) {
                if (event.inputType === 'insertCompositionText') {
                    return;
                }
                if (event.data) {
                    this.commitKeyboardText(event.data);
                    return;
                }
            }
            this.syncKeyboardInputToDevice();
        });

        input.addEventListener('keydown', (event) => {
            if (!this.keyboardMappingEnabled) {
                return;
            }
            if (event.isComposing || event.keyCode === 229) {
                return;
            }

            const androidKey = this.mapToAndroidKeyCode(event);
            if (androidKey != null && ScrcpyInput.FUNCTION_KEY_CODES.has(event.code)) {
                this.sendKeyCode(event, 0, androidKey);
                this._pressedKeys.add(androidKey);
                event.preventDefault();
                return;
            }

            if (event.key === 'Enter') {
                this.sendKeyCode(event, 0, 66);
                this.sendKeyCode(event, 1, 66);
                event.preventDefault();
            } else if (event.key === 'Backspace') {
                event.preventDefault();
                this.sendKeyCode(event, 0, 67);
                this.sendKeyCode(event, 1, 67);
                if (this.keyboardInputLastValue.length > 0) {
                    this.keyboardInputLastValue = this.keyboardInputLastValue.slice(0, -1);
                    input.value = this.keyboardInputLastValue;
                }
            }
        });

        input.addEventListener('keyup', (event) => {
            if (!this.keyboardMappingEnabled || event.isComposing) {
                return;
            }
            const androidKey = this.mapToAndroidKeyCode(event);
            if (androidKey != null && this._pressedKeys.has(androidKey)) {
                this.sendKeyCode(event, 1, androidKey);
                this._pressedKeys.delete(androidKey);
                event.preventDefault();
            }
        });

        input.addEventListener('blur', () => {
            this.keyboardInputActive = false;
            input.classList.remove('is-active');
            this.releasePressedKeys();
            this.notifyFocusChange(false);
        });
    }

    bindDocumentKeyboardEvents() {
        this._onDocumentKeyDown = (event) => {
            if (!this.keyboardMappingEnabled || !this.keyboardInputElement) {
                return;
            }
            if (document.activeElement === this.keyboardInputElement) {
                return;
            }
            if (['Shift', 'Control', 'Alt', 'Meta'].includes(event.key)) {
                return;
            }
            this.activateKeyboardInput();
        };
        document.addEventListener('keydown', this._onDocumentKeyDown, true);
    }

    releasePressedKeys() {
        for (const keycode of this._pressedKeys) {
            this.sendKeyCode(null, 1, keycode);
        }
        this._pressedKeys.clear();
    }

    ensureKeyboardInputFocused() {
        if (!this.keyboardMappingEnabled || !this.keyboardInputElement) {
            return;
        }
        this.activateKeyboardInput();
        requestAnimationFrame(() => this.activateKeyboardInput());
        setTimeout(() => this.activateKeyboardInput(), 50);
    }

    commitKeyboardText(text) {
        if (!this.keyboardMappingEnabled || !text) {
            return;
        }
        this.sendText(text);
        if (this.keyboardInputElement) {
            this.keyboardInputElement.value = '';
            this.keyboardInputLastValue = '';
        }
    }

    syncKeyboardInputToDevice() {
        if (!this.keyboardInputElement) {
            return;
        }

        const input = this.keyboardInputElement;
        const value = input.value;

        if (value.length > this.keyboardInputLastValue.length) {
            const added = value.slice(this.keyboardInputLastValue.length);
            this.sendText(added);
        } else if (value.length < this.keyboardInputLastValue.length) {
            const removed = this.keyboardInputLastValue.length - value.length;
            for (let i = 0; i < removed; i++) {
                this.sendKeyCode(null, 0, 67);
                this.sendKeyCode(null, 1, 67);
            }
        }

        input.value = '';
        this.keyboardInputLastValue = '';
    }

    static hasNonAscii(text) {
        for (let i = 0; i < text.length; i++) {
            if (text.charCodeAt(i) > 127) {
                return true;
            }
        }
        return false;
    }

    sendText(text) {
        if (!text) {
            return;
        }
        const data = ScrcpyInput.hasNonAscii(text)
            ? this.createClipboardPasteProtocolData(text)
            : this.createTextProtocolData(text);
        if (data) {
            this.callback(data);
        }
    }

    notifyFocusChange(active) {
        if (this.onFocusChange) {
            this.onFocusChange(active);
        }
    }

    focusVideo() {
        if (typeof this.videoElement.focus === 'function') {
            this.videoElement.focus({ preventScroll: true });
        }
    }

    tryInitialFocus() {
        if (this.hasFocusedOnce || !this.keyboardMappingEnabled || this.isTouchPrimary) {
            return;
        }
        this.hasFocusedOnce = true;
        this.activateKeyboardInput();
    }

    activateKeyboardInput() {
        if (!this.keyboardInputElement || !this.keyboardMappingEnabled || this.isTouchPrimary) {
            return;
        }

        const input = this.keyboardInputElement;
        const wasActive = this.keyboardInputActive;
        this.keyboardInputActive = true;
        input.classList.add('is-active');
        if (!wasActive) {
            input.value = '';
            this.keyboardInputLastValue = '';
            this.isComposing = false;
        }
        input.focus({ preventScroll: true });
        this.notifyFocusChange(true);
    }

    deactivateKeyboardInput() {
        if (!this.keyboardInputElement) {
            return;
        }

        const input = this.keyboardInputElement;
        this.keyboardInputActive = false;
        this.isComposing = false;
        input.classList.remove('is-active');
        this.releasePressedKeys();
        input.blur();
        input.value = '';
        this.keyboardInputLastValue = '';
    }

    setKeyboardMappingEnabled(enabled) {
        if (this.isTouchPrimary) {
            this.keyboardMappingEnabled = false;
            this.deactivateKeyboardInput();
            this.notifyFocusChange(false);
            return;
        }

        this.keyboardMappingEnabled = enabled;
        if (!enabled) {
            this.deactivateKeyboardInput();
            this.notifyFocusChange(false);
            return;
        }

        this.activateKeyboardInput();
        this.ensureKeyboardInputFocused();
    }

    sendKeyCode(keyevent, action, keycode) {
        const getModifierState = (name) => {
            if (keyevent && typeof keyevent.getModifierState === 'function') {
                return keyevent.getModifierState(name);
            }
            return false;
        };

        let metakey = 0;
        if (keyevent && keyevent.shiftKey) metakey |= 0x40;
        if (keyevent && keyevent.ctrlKey) metakey |= 0x2000;
        if (keyevent && keyevent.altKey) metakey |= 0x10;
        if (keyevent && keyevent.metaKey) metakey |= 0x20000;
        if (getModifierState('CapsLock')) metakey |= 0x100000;
        if (getModifierState('NumLock')) metakey |= 0x200000;

        const repeat = keyevent && keyevent.repeat ? 1 : 0;
        this.callback(this.createKeyProtocolData(action, keycode, repeat, metakey));
    }

    getDisplayMetrics() {
        const rect = this.videoElement.getBoundingClientRect();
        let videoW = this.width;
        let videoH = this.height;
        if (this.getMediaSize) {
            const size = this.getMediaSize();
            if (size && size.width && size.height) {
                videoW = size.width;
                videoH = size.height;
            }
        } else if (this.videoElement.videoWidth && this.videoElement.videoHeight) {
            videoW = this.videoElement.videoWidth;
            videoH = this.videoElement.videoHeight;
        } else if (this.videoElement.tagName === 'CANVAS'
            && this.videoElement.width && this.videoElement.height) {
            videoW = this.videoElement.width;
            videoH = this.videoElement.height;
        }
        const scale = Math.min(rect.width / videoW, rect.height / videoH);
        const displayW = videoW * scale;
        const displayH = videoH * scale;
        return {
            rect,
            offsetX: (rect.width - displayW) / 2,
            offsetY: (rect.height - displayH) / 2,
            displayW,
            displayH
        };
    }

    clientToDevice(clientX, clientY) {
        const { rect, offsetX, offsetY, displayW, displayH } = this.getDisplayMetrics();
        const localX = clientX - rect.left - offsetX;
        const localY = clientY - rect.top - offsetY;

        if (localX < 0 || localY < 0 || localX > displayW || localY > displayH) {
            return null;
        }

        return {
            x: Math.round((localX / displayW) * this.width),
            y: Math.round((localY / displayH) * this.height)
        };
    }

    updatePointer(clientX, clientY) {
        const pos = this.clientToDevice(clientX, clientY);
        if (!pos) {
            return false;
        }
        this.pointerX = pos.x;
        this.pointerY = pos.y;
        return true;
    }

    sendTouch(action, pressure, options = {}) {
        const pointerId = options.pointerId != null ? options.pointerId : ScrcpyInput.POINTER_ID_MOUSE;
        const actionButton = options.actionButton || 0;
        const buttons = options.buttons || 0;
        const data = this.createTouchProtocolData(
            action, this.pointerX, this.pointerY,
            this.width, this.height, actionButton, buttons, pressure, pointerId
        );
        this.callback(data);
    }

    onPointerDown(event) {
        if (event.button === 0) {
            if (!this.updatePointer(event.clientX, event.clientY)) {
                return;
            }
            this.isPointerDown = true;
            this.sendTouch(0, 0xFFFF, {
                actionButton: ScrcpyInput.BUTTON_PRIMARY,
                buttons: ScrcpyInput.BUTTON_PRIMARY,
            });
            event.preventDefault();
        } else if (event.button === 2) {
            this.rightButtonIsPressed = true;
            this.sendKeyCode(event, 0, 4);
            event.preventDefault();
        }
    }

    onPointerMove(event) {
        if (!this.isPointerDown) {
            return;
        }
        if (!this.updatePointer(event.clientX, event.clientY)) {
            return;
        }
        this.sendTouch(2, 0xFFFF, {
            buttons: ScrcpyInput.BUTTON_PRIMARY,
        });
        event.preventDefault();
    }

    onPointerUp(event) {
        if (event.button === 0 && this.isPointerDown) {
            this.isPointerDown = false;
            this.updatePointer(event.clientX, event.clientY);
            this.sendTouch(1, 0, {
                actionButton: ScrcpyInput.BUTTON_PRIMARY,
                buttons: 0,
            });
            event.preventDefault();
        } else if (event.button === 2 && this.rightButtonIsPressed) {
            this.rightButtonIsPressed = false;
            this.sendKeyCode(event, 1, 4);
            event.preventDefault();
        }
    }

    getTouchPoint(event) {
        let touch = null;
        if (this.activeTouchId !== null) {
            for (const item of event.changedTouches) {
                if (item.identifier === this.activeTouchId) {
                    touch = item;
                    break;
                }
            }
        }
        if (!touch && event.touches.length > 0) {
            touch = event.touches[0];
        }
        if (!touch && event.changedTouches.length > 0) {
            touch = event.changedTouches[0];
        }
        return touch;
    }

    preventTouchDefault(event) {
        if (event.cancelable) {
            event.preventDefault();
        }
    }

    onTouchStart(event) {
        if (this.activeTouchId !== null) {
            return;
        }
        const touch = event.changedTouches[0];
        if (!touch) {
            return;
        }
        this.activeTouchId = touch.identifier;
        if (!this.updatePointer(touch.clientX, touch.clientY)) {
            this.activeTouchId = null;
            return;
        }
        this.isPointerDown = true;
        this.sendTouch(0, 0xFFFF, {
            pointerId: ScrcpyInput.POINTER_ID_FINGER,
            actionButton: ScrcpyInput.BUTTON_PRIMARY,
            buttons: ScrcpyInput.BUTTON_PRIMARY,
        });
        this.preventTouchDefault(event);
    }

    onTouchMove(event) {
        if (this.activeTouchId === null) {
            return;
        }
        const touch = this.getTouchPoint(event);
        if (!touch) {
            return;
        }
        if (!this.updatePointer(touch.clientX, touch.clientY)) {
            return;
        }
        this.sendTouch(2, 0xFFFF, {
            pointerId: ScrcpyInput.POINTER_ID_FINGER,
            buttons: ScrcpyInput.BUTTON_PRIMARY,
        });
        this.preventTouchDefault(event);
    }

    onTouchEnd(event) {
        if (this.activeTouchId === null) {
            return;
        }
        let ended = false;
        for (const touch of event.changedTouches) {
            if (touch.identifier === this.activeTouchId) {
                this.updatePointer(touch.clientX, touch.clientY);
                ended = true;
                break;
            }
        }
        if (!ended) {
            return;
        }
        this.isPointerDown = false;
        this.activeTouchId = null;
        this.sendTouch(1, 0, {
            pointerId: ScrcpyInput.POINTER_ID_FINGER,
            actionButton: ScrcpyInput.BUTTON_PRIMARY,
            buttons: 0,
        });
        this.preventTouchDefault(event);
    }

    resizeScreen(width, height) {
        this.width = width;
        this.height = height;
    }

    mapToAndroidKeyCode(event) {
        const codeToAndroidKeyCode = {
            'KeyA': 29, 'KeyB': 30, 'KeyC': 31, 'KeyD': 32, 'KeyE': 33,
            'KeyF': 34, 'KeyG': 35, 'KeyH': 36, 'KeyI': 37, 'KeyJ': 38,
            'KeyK': 39, 'KeyL': 40, 'KeyM': 41, 'KeyN': 42, 'KeyO': 43,
            'KeyP': 44, 'KeyQ': 45, 'KeyR': 46, 'KeyS': 47, 'KeyT': 48,
            'KeyU': 49, 'KeyV': 50, 'KeyW': 51, 'KeyX': 52, 'KeyY': 53,
            'KeyZ': 54,
            'Digit0': 7, 'Digit1': 8, 'Digit2': 9, 'Digit3': 10, 'Digit4': 11,
            'Digit5': 12, 'Digit6': 13, 'Digit7': 14, 'Digit8': 15, 'Digit9': 16,
            'Enter': 66, 'Backspace': 67, 'Tab': 61, 'Space': 62, 'Escape': 111,
            'CapsLock': 115, 'NumLock': 143, 'ScrollLock': 116,
            'ArrowUp': 19, 'ArrowDown': 20, 'ArrowLeft': 21, 'ArrowRight': 22,
            'ShiftLeft': 59, 'ShiftRight': 60, 'ControlLeft': 113, 'ControlRight': 114,
            'AltLeft': 57, 'AltRight': 58, 'MetaLeft': 117, 'MetaRight': 118,
            'Delete': 112, 'Insert': 124, 'Home': 122, 'End': 123,
            'PageUp': 92, 'PageDown': 93,
            'Minus': 69, 'Equal': 70, 'BracketLeft': 71, 'BracketRight': 72,
            'Backslash': 73, 'Semicolon': 74, 'Quote': 75, 'Backquote': 68,
            'Comma': 55, 'Period': 56, 'Slash': 76,
            'Numpad0': 144, 'Numpad1': 145, 'Numpad2': 146, 'Numpad3': 147,
            'Numpad4': 148, 'Numpad5': 149, 'Numpad6': 150, 'Numpad7': 151,
            'Numpad8': 152, 'Numpad9': 153, 'NumpadEnter': 160,
            'NumpadAdd': 157, 'NumpadSubtract': 156, 'NumpadMultiply': 155,
            'NumpadDivide': 154, 'NumpadDecimal': 158,
            'F1': 131, 'F2': 132, 'F3': 133, 'F4': 134, 'F5': 135, 'F6': 136,
            'F7': 137, 'F8': 138, 'F9': 139, 'F10': 140, 'F11': 141, 'F12': 142,
            'ContextMenu': 82, 'PrintScreen': 120, 'Pause': 121,
        };

        const androidKeyCode = codeToAndroidKeyCode[event.code];
        return androidKeyCode !== undefined ? androidKeyCode : null;
    }

    createTouchProtocolData(action, x, y, width, height, actionButton, buttons, pressure, pointerId) {
        const type = 2;
        const buffer = new ArrayBuffer(32);
        const view = new DataView(buffer);
        const id = pointerId != null ? BigInt(pointerId) : ScrcpyInput.POINTER_ID_MOUSE;

        let offset = 0;
        view.setUint8(offset, type); offset += 1;
        view.setUint8(offset, action); offset += 1;
        view.setBigUint64(offset, id, false); offset += 8;

        view.setInt32(offset, x, false); offset += 4;
        view.setInt32(offset, y, false); offset += 4;
        view.setUint16(offset, width, false); offset += 2;
        view.setUint16(offset, height, false); offset += 2;
        view.setUint16(offset, pressure & 0xFFFF, false); offset += 2;
        view.setInt32(offset, actionButton, false); offset += 4;
        view.setInt32(offset, buttons, false);

        return buffer;
    }

    createKeyProtocolData(action, keycode, repeat, metaState) {
        const type = 0;
        const buffer = new ArrayBuffer(14);
        const view = new DataView(buffer);

        let offset = 0;
        view.setUint8(offset, type); offset += 1;
        view.setUint8(offset, action); offset += 1;
        view.setInt32(offset, keycode, false); offset += 4;
        view.setInt32(offset, repeat, false); offset += 4;
        view.setInt32(offset, metaState, false);

        return buffer;
    }

    createTextProtocolData(text) {
        const bytes = ScrcpyInput.utf8ByteLengthSafe(text, ScrcpyInput.INJECT_TEXT_MAX_LENGTH);
        const length = bytes.length;
        const buffer = new ArrayBuffer(1 + 4 + length);
        const view = new DataView(buffer);

        view.setUint8(0, ScrcpyInput.TYPE_INJECT_TEXT);
        view.setUint32(1, length, false);
        new Uint8Array(buffer, 5, length).set(bytes);

        return buffer;
    }

    createClipboardPasteProtocolData(text) {
        const bytes = ScrcpyInput.utf8ByteLengthSafe(text, ScrcpyInput.INJECT_TEXT_MAX_LENGTH);
        const length = bytes.length;
        const buffer = new ArrayBuffer(1 + 8 + 1 + 4 + length);
        const view = new DataView(buffer);

        let offset = 0;
        view.setUint8(offset, ScrcpyInput.TYPE_SET_CLIPBOARD);
        offset += 1;

        this.clipboardSequence += 1;
        const seq = BigInt(this.clipboardSequence);
        view.setUint32(offset, Number(seq >> 32n), false);
        offset += 4;
        view.setUint32(offset, Number(seq & 0xffffffffn), false);
        offset += 4;

        view.setUint8(offset, 1);
        offset += 1;
        view.setUint32(offset, length, false);
        offset += 4;
        new Uint8Array(buffer, offset, length).set(bytes);

        return buffer;
    }

    createScrollProtocolData(x, y, width, height, hScroll, vScroll, buttons) {
        const type = 3;
        const buffer = new ArrayBuffer(21);
        const view = new DataView(buffer);

        let offset = 0;
        view.setUint8(offset, type); offset += 1;
        view.setInt32(offset, x, false); offset += 4;
        view.setInt32(offset, y, false); offset += 4;
        view.setUint16(offset, width, false); offset += 2;
        view.setUint16(offset, height, false); offset += 2;
        view.setInt16(offset, ScrcpyInput.wheelDeltaToScrollFp(hScroll), false); offset += 2;
        view.setInt16(offset, ScrcpyInput.wheelDeltaToScrollFp(vScroll), false); offset += 2;
        view.setInt32(offset, buttons || 0, false);

        return buffer;
    }
}
