/**
 * 混排输入：块状态为唯一数据源，渲染到单一 contenteditable。
 * 键盘/光标完全由块模型驱动，附件为原子单位。
 */
(function (global) {
  function uid() {
    return crypto.randomUUID();
  }

  function collectPasteFiles(cd) {
    const files = [...(cd?.files || [])];
    if (!files.length && cd?.items) {
      for (const item of cd.items) {
        if (item.kind === "file") {
          const f = item.getAsFile();
          if (f) files.push(f);
        }
      }
    }
    return files;
  }

  function formatLabel(item) {
    if (item.path) {
      const parts = String(item.path).replace(/\/+$/, "").split("/");
      const base = parts.pop() || item.name || "引用";
      return item.kind === "directory" ? `${base}/` : base;
    }
    return item.name || "引用";
  }

  function encodeMarker(path) {
    return `[[πattach:${path}]]`;
  }

  function stripZwsp(text) {
    return String(text || "").replace(/\u200b/g, "");
  }

  function domTextForBlock(value) {
    return value.length ? value : "\u200b";
  }

  function logicalOffsetToDom(textNode, logicalOffset) {
    const raw = textNode.textContent || "";
    if (!stripZwsp(raw)) return 0;
    return logicalOffset;
  }

  function domOffsetToLogical(textNode, domOffset) {
    const raw = textNode.textContent || "";
    if (!stripZwsp(raw)) return 0;
    const prefix = raw.startsWith("\u200b") ? 1 : 0;
    return Math.max(0, domOffset - prefix);
  }

  function textBlock(value = "") {
    return { type: "text", id: uid(), value };
  }

  function attachBlock(data) {
    return { type: "attachment", id: uid(), ...data };
  }

  class AgentComposer {
    constructor(options = {}) {
      this.root = options.rootEl;
      this.onUploadingChange = options.onUploadingChange || (() => {});
      this.onEnter = options.onEnter || (() => {});
      this.pending = new Map();
      this.uploading = false;
      this.blocks = [textBlock()];
      this.caret = 0;
      this._rendering = false;
      this._composing = false;
      this._lastCompositionData = "";
      this._compositionRange = null;
      this._compositionCaret = null;

      if (!this.root) return;

      this.root.contentEditable = "true";
      this.root.setAttribute("role", "textbox");
      this.root.setAttribute("aria-multiline", "true");
      this.root.spellcheck = false;

      this.root.addEventListener("keydown", (e) => this._onKeyDown(e));
      this.root.addEventListener("beforeinput", (e) => this._onBeforeInput(e));
      this.root.addEventListener("paste", (e) => this._onPaste(e));
      this.root.addEventListener("click", () => this._syncCaretFromSelection());
      this.root.addEventListener("mouseup", () => this._syncCaretFromSelection());
      this.root.addEventListener("compositionstart", () => {
        const range = this._getSelectionCaretRange();
        this._compositionRange = range && range.end > range.start ? range : null;
        if (this._compositionRange) {
          this.caret = this._compositionRange.start;
        } else {
          this._syncCaretFromSelection();
        }
        this._compositionCaret = this.caret;
        this._composing = true;
        this._lastCompositionData = "";
        this._prepareCompositionDom();
        this.root.classList.add("is-composing");
        this.root.classList.remove("is-empty");
      });
      this.root.addEventListener("compositionupdate", () => {
        this.root.classList.add("is-composing");
        this.root.classList.remove("is-empty");
      });
      this.root.addEventListener("compositionend", (e) => {
        const data = e.data || "";
        this._lastCompositionData = data;
        this._composing = false;
        this.root.classList.remove("is-composing");
        const range = this._compositionRange;
        this._compositionRange = null;
        const startCaret = this._compositionCaret;
        this._compositionCaret = null;
        if (data) {
          if (range) {
            this._deleteRange(range.start, range.end);
          } else if (startCaret != null) {
            this.caret = Math.min(startCaret, this._maxCaret());
          }
          this._insertText(data);
        } else {
          this._render();
        }
        queueMicrotask(() => {
          this._lastCompositionData = "";
        });
      });

      this._render();
      requestAnimationFrame(() => this.focus());
    }

    focus() {
      if (!this.root) return;
      if (document.activeElement !== this.root) {
        this.root.focus({ preventScroll: true });
      }
      if (this._composing) return;
      requestAnimationFrame(() => {
        if (!this._composing) this._applyCaret();
      });
    }

    isBlocked() {
      return this.uploading;
    }

    isEmpty() {
      return !this._hasContent();
    }

    clear() {
      for (const entry of this.pending.values()) {
        if (entry?.objectUrl) URL.revokeObjectURL(entry.objectUrl);
      }
      this.pending.clear();
      this.uploading = false;
      this.onUploadingChange(false);
      this.blocks = [textBlock()];
      this.caret = 0;
      this._render();
      this.focus();
    }

    insertText(text) {
      if (!text) return;
      this._insertText(text);
    }

    addFile(file) {
      if (!file) return;
      const localId = `local:${uid()}`;
      const previewUrl = file.type?.startsWith("image/") ? URL.createObjectURL(file) : null;
      this.pending.set(localId, { file, objectUrl: previewUrl });
      this._insertAttachment(
        attachBlock({
          localId,
          name: file.name || "图片",
          kind: "file",
          previewUrl,
          pending: true,
          markerPath: localId,
        })
      );
    }

    addPath(item) {
      if (!item?.path) return;
      const path = String(item.path);
      this._insertAttachment(
        attachBlock({
          path,
          name: item.name || formatLabel(item),
          kind: item.kind || "file",
          pending: false,
          markerPath: path,
        })
      );
    }

    addPathAttachments(items) {
      for (const item of items || []) this.addPath(item);
    }

    addReferencedAttachments(attachments) {
      this.importPlainWithAttachments("", attachments || []);
    }

    importPlainWithAttachments(plain, attachments) {
      if (!attachments?.length) {
        if (plain) this.insertText(plain);
        return;
      }
      let remaining = plain || "";
      for (const item of attachments) {
        const path = String(item.path || "").trim();
        if (!path) continue;
        const idx = remaining.indexOf(path);
        if (idx >= 0) {
          if (idx > 0) this._insertText(remaining.slice(0, idx));
          this._insertAttachment(
            attachBlock({
              path,
              name: item.name || formatLabel(item),
              kind: item.kind || "file",
              pending: false,
              markerPath: path,
            })
          );
          remaining = remaining.slice(idx + path.length);
        }
      }
      if (remaining) this._insertText(remaining);
      if (!plain && attachments.length) {
        for (const item of attachments) {
          if (item?.path) this.addPath(item);
        }
      }
    }

    handlePaste(cd) {
      const files = collectPasteFiles(cd);
      if (!files.length) return false;
      for (const file of files) this.addFile(file);
      return true;
    }

    async uploadPending(agentId) {
      const pendingBlocks = this.blocks.filter((b) => b.type === "attachment" && b.pending);
      if (!pendingBlocks.length) return;

      this.uploading = true;
      this.onUploadingChange(true);
      try {
        for (const block of pendingBlocks) {
          const entry = this.pending.get(block.localId);
          if (!entry?.file) continue;

          const form = new FormData();
          form.append("agent_id", agentId);
          form.append("file", entry.file);
          const res = await fetch("/api/upload", { method: "POST", body: form });
          const data = await res.json();
          if (!data.ok) throw new Error(data.detail || "上传失败");

          if (entry.objectUrl) URL.revokeObjectURL(entry.objectUrl);
          this.pending.delete(block.localId);

          block.path = data.path;
          block.name = data.name;
          block.markerPath = data.path;
          block.pending = false;
          block.previewUrl = null;
          delete block.localId;
        }
        this._render();
      } finally {
        this.uploading = false;
        this.onUploadingChange(false);
      }
    }

    serialize() {
      let plain = "";
      let serialized = "";
      const attachments = [];
      const seen = new Set();

      for (const block of this.blocks) {
        if (block.type === "text") {
          plain += block.value;
          serialized += block.value;
          continue;
        }
        const path = block.markerPath || block.path || block.localId || "";
        if (!path) continue;
        serialized += encodeMarker(path);
        if (!block.pending && block.path && !seen.has(block.path)) {
          seen.add(block.path);
          attachments.push({
            path: block.path,
            kind: block.kind || "file",
            name: block.name || formatLabel(block),
          });
        }
      }

      return { plain, serialized, attachments };
    }

    /* ---- caret model: index 0..max, attachments count as 1 slot ---- */

    _maxCaret() {
      let n = 0;
      for (const b of this.blocks) {
        if (b.type === "text") n += b.value.length;
        else n += 1;
      }
      return n;
    }

    _indexToPos(index) {
      let remaining = index;
      for (const b of this.blocks) {
        if (b.type === "text") {
          if (remaining <= b.value.length) {
            return { kind: "text", blockId: b.id, offset: remaining };
          }
          remaining -= b.value.length;
        } else {
          if (remaining === 0) return { kind: "attach", blockId: b.id, side: "before" };
          remaining -= 1;
          if (remaining === 0) return { kind: "attach", blockId: b.id, side: "after" };
        }
      }
      return { kind: "end" };
    }

    _posToIndex(pos) {
      let index = 0;
      for (const b of this.blocks) {
        if (b.type === "text") {
          if (pos.kind === "text" && pos.blockId === b.id) {
            return index + pos.offset;
          }
          index += b.value.length;
        } else {
          if (pos.kind === "attach" && pos.blockId === b.id) {
            return pos.side === "before" ? index : index + 1;
          }
          index += 1;
        }
      }
      return index;
    }

    _prepareCompositionDom() {
      const pos = this._indexToPos(this.caret);
      if (pos.kind !== "text") return;
      const span = this.root.querySelector(`[data-block-id="${pos.blockId}"]`);
      const textNode = span?.firstChild;
      if (textNode?.nodeType !== Node.TEXT_NODE) return;
      if (!stripZwsp(textNode.textContent)) {
        textNode.textContent = "";
      }
    }

    _insertText(str) {
      this._deleteSelectionIfAny();
      const pos = this._indexToPos(this.caret);
      if (pos.kind === "text") {
        const block = this.blocks.find((b) => b.id === pos.blockId);
        if (!block) return;
        block.value = block.value.slice(0, pos.offset) + str + block.value.slice(pos.offset);
        this.caret += str.length;
      } else if (pos.kind === "attach") {
        const idx = this.blocks.findIndex((b) => b.id === pos.blockId);
        const newBlock = textBlock(str);
        if (pos.side === "before") {
          this.blocks.splice(idx, 0, newBlock);
          this.caret = this._posToIndex({ kind: "text", blockId: newBlock.id, offset: str.length });
        } else {
          this.blocks.splice(idx + 1, 0, newBlock);
          this.caret = this._posToIndex({ kind: "text", blockId: newBlock.id, offset: str.length });
        }
      } else {
        const last = this.blocks[this.blocks.length - 1];
        if (last?.type === "text") {
          last.value += str;
        } else {
          this.blocks.push(textBlock(str));
        }
        this.caret = this._maxCaret();
      }
      this._mergeAdjacentText();
      this._render();
    }

    _insertAttachment(block) {
      const pos = this._indexToPos(this.caret);
      let insertAt = this.blocks.length;

      if (pos.kind === "text") {
        const idx = this.blocks.findIndex((b) => b.id === pos.blockId);
        const tb = this.blocks[idx];
        const before = tb.value.slice(0, pos.offset);
        const after = tb.value.slice(pos.offset);
        const parts = [];
        if (before) parts.push(textBlock(before));
        parts.push(block);
        if (after) parts.push(textBlock(after));
        else parts.push(textBlock(""));
        this.blocks.splice(idx, 1, ...parts);
        insertAt = idx + parts.length - 1;
      } else if (pos.kind === "attach") {
        const idx = this.blocks.findIndex((b) => b.id === pos.blockId);
        insertAt = pos.side === "before" ? idx : idx + 1;
        this.blocks.splice(insertAt, 0, block, textBlock(""));
      } else {
        this.blocks.push(block, textBlock(""));
        insertAt = this.blocks.length - 1;
      }

      this._mergeAdjacentText();
      this.caret = this._posToIndex({ kind: "attach", blockId: block.id, side: "after" });
      this._render();
    }

    _revokeAttachmentBlock(block) {
      if (!block?.localId) return;
      const entry = this.pending.get(block.localId);
      if (entry?.objectUrl) URL.revokeObjectURL(entry.objectUrl);
      this.pending.delete(block.localId);
    }

    _getSelectionCaretRange() {
      const sel = window.getSelection();
      if (!sel?.rangeCount || sel.isCollapsed) return null;
      if (!this.root.contains(sel.anchorNode) || !this.root.contains(sel.focusNode)) return null;

      const anchorIdx = this._domPointToCaret(sel.anchorNode, sel.anchorOffset);
      const focusIdx = this._domPointToCaret(sel.focusNode, sel.focusOffset);
      if (anchorIdx == null || focusIdx == null) return null;
      return { start: Math.min(anchorIdx, focusIdx), end: Math.max(anchorIdx, focusIdx) };
    }

    _deleteSelectionIfAny() {
      const range = this._getSelectionCaretRange();
      if (!range) return false;
      this._deleteRange(range.start, range.end);
      return true;
    }

    _childCaretLength(node) {
      if (node?.classList?.contains("composer-text-run")) {
        return node.textContent?.length || 0;
      }
      if (node?.classList?.contains("composer-inline-chip")) return 1;
      return 0;
    }

    _domPointToCaret(node, offset) {
      if (!node || !this.root) return null;
      const el = node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement;
      if (!el || !this.root.contains(el)) return null;

      if (node === this.root) {
        let idx = 0;
        const limit = Math.min(offset, this.root.childNodes.length);
        for (let i = 0; i < limit; i += 1) {
          idx += this._childCaretLength(this.root.childNodes[i]);
        }
        return idx;
      }

      const textRun =
        node.nodeType === Node.TEXT_NODE
          ? node.parentElement?.closest?.(".composer-text-run")
          : node.closest?.(".composer-text-run");
      if (textRun) {
        const blockId = textRun.dataset.blockId;
        let off = 0;
        if (node.nodeType === Node.TEXT_NODE && textRun.contains(node)) {
          off = domOffsetToLogical(node, offset);
        } else if (textRun.firstChild?.nodeType === Node.TEXT_NODE) {
          off = stripZwsp(textRun.firstChild.textContent).length;
        }
        return this._posToIndex({ kind: "text", blockId, offset: off });
      }

      const chip =
        node.nodeType === Node.ELEMENT_NODE
          ? node.closest?.(".composer-inline-chip")
          : node.parentElement?.closest?.(".composer-inline-chip");
      if (chip) {
        return this._posToIndex({
          kind: "attach",
          blockId: chip.dataset.blockId,
          side: offset === 0 ? "before" : "after",
        });
      }

      return null;
    }

    _deleteRange(start, end) {
      if (start >= end) return;
      if (start === 0 && end >= this._maxCaret()) {
        this.clear();
        return;
      }

      const result = [];
      let cursor = 0;

      for (const b of this.blocks) {
        if (b.type === "text") {
          const len = b.value.length;
          const bStart = cursor;
          const bEnd = cursor + len;

          if (bEnd <= start || bStart >= end) {
            result.push(b);
          } else {
            const sliceStart = Math.max(0, start - bStart);
            const sliceEnd = Math.min(len, end - bStart);
            const left = b.value.slice(0, sliceStart);
            const right = b.value.slice(sliceEnd);
            if (left) result.push(textBlock(left));
            if (right) result.push(textBlock(right));
          }
          cursor = bEnd;
        } else {
          const bStart = cursor;
          if (bStart >= start && bStart + 1 <= end) {
            this._revokeAttachmentBlock(b);
          } else {
            result.push(b);
          }
          cursor += 1;
        }
      }

      this.blocks = result.length ? result : [textBlock()];
      this._mergeAdjacentText();
      this._ensureTextBlock();
      this.caret = Math.min(start, this._maxCaret());
      this._render();
    }

    _deleteBackward() {
      if (this._deleteSelectionIfAny()) return;
      if (this.caret <= 0) return;
      const pos = this._indexToPos(this.caret);
      if (pos.kind === "text" && pos.offset > 0) {
        const block = this.blocks.find((b) => b.id === pos.blockId);
        block.value = block.value.slice(0, pos.offset - 1) + block.value.slice(pos.offset);
        this.caret -= 1;
        this._cleanupEmptyText();
        this._render();
        return;
      }
      if (pos.kind === "text" && pos.offset === 0) {
        const idx = this.blocks.findIndex((b) => b.id === pos.blockId);
        const prev = this.blocks[idx - 1];
        if (prev?.type === "text") {
          this.caret = this._posToIndex({ kind: "text", blockId: prev.id, offset: prev.value.length });
          this._deleteBackward();
          return;
        }
        if (prev?.type === "attachment") {
          this._removeBlock(prev.id);
          return;
        }
      }
      if (pos.kind === "attach" && pos.side === "after") {
        this._removeBlock(pos.blockId);
        return;
      }
      this.caret -= 1;
      const newPos = this._indexToPos(this.caret);
      if (newPos.kind === "attach" && newPos.side === "after") {
        this._removeBlock(newPos.blockId);
      } else if (newPos.kind === "text" && newPos.offset > 0) {
        this._deleteBackward();
      }
    }

    _removeBlock(blockId) {
      const idx = this.blocks.findIndex((b) => b.id === blockId);
      if (idx < 0) return;
      const block = this.blocks[idx];
      if (block.type === "attachment") {
        this._revokeAttachmentBlock(block);
        const prev = this.blocks[idx - 1];
        const next = this.blocks[idx + 1];
        this.blocks.splice(idx, 1);
        if (prev?.type === "text" && next?.type === "text") {
          const mergeAt = prev.value.length;
          prev.value += next.value;
          this.blocks.splice(this.blocks.indexOf(next), 1);
          this.caret = this._posToIndex({ kind: "text", blockId: prev.id, offset: mergeAt });
        } else {
          this.caret = this._posToIndex(this._indexToPos(Math.min(this.caret, this._maxCaret())));
        }
      }
      this._ensureTextBlock();
      this._render();
    }

    _mergeAdjacentText() {
      const merged = [];
      for (const b of this.blocks) {
        if (b.type !== "text") {
          merged.push(b);
          continue;
        }
        const last = merged[merged.length - 1];
        if (last?.type === "text") last.value += b.value;
        else merged.push(b);
      }
      this.blocks = merged.length ? merged : [textBlock()];
    }

    _cleanupEmptyText() {
      this.blocks = this.blocks.filter((b) => b.type !== "text" || b.value.length > 0 || this.blocks.length === 1);
      this._ensureTextBlock();
    }

    _ensureTextBlock() {
      if (!this.blocks.length) this.blocks = [textBlock()];
      if (!this.blocks.some((b) => b.type === "text")) {
        this.blocks.push(textBlock(""));
      }
    }

    _hasContent() {
      return this.blocks.some((b) => (b.type === "text" && b.value.trim()) || b.type === "attachment");
    }

    _onKeyDown(e) {
      if (e.isComposing || this._composing) return;

      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        this.onEnter();
        return;
      }
      if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
        if (this._caretInExpandedLabel()) return;
        e.preventDefault();
        if (e.key === "ArrowLeft") {
          this.caret = Math.max(0, this.caret - 1);
        } else {
          this.caret = Math.min(this._maxCaret(), this.caret + 1);
        }
        this._applyCaret();
      }
    }

    _deleteForward() {
      if (this._deleteSelectionIfAny()) return;
      if (this.caret >= this._maxCaret()) return;
      this._deleteRange(this.caret, this.caret + 1);
    }

    _caretInExpandedLabel() {
      const sel = window.getSelection();
      if (!sel?.rangeCount || !sel.isCollapsed) return false;
      const node = sel.anchorNode;
      const el = node?.nodeType === Node.ELEMENT_NODE ? node : node?.parentElement;
      return !!el?.closest?.(".composer-inline-chip.show-full-path .composer-inline-label");
    }

    _onBeforeInput(e) {
      if (e.isComposing || this._composing) return;

      if (e.inputType === "insertText" || e.inputType === "insertReplacementText") {
        e.preventDefault();
        const data = e.data || "";
        if (!data) return;
        if (data === this._lastCompositionData) return;
        this._insertText(data);
        return;
      }
      if (e.inputType === "insertLineBreak" || e.inputType === "insertParagraph") {
        e.preventDefault();
        this._insertText("\n");
        return;
      }
      if (e.inputType === "deleteContentBackward") {
        e.preventDefault();
        this._deleteBackward();
        return;
      }
      if (e.inputType === "deleteContentForward") {
        e.preventDefault();
        this._deleteForward();
        return;
      }
      if (e.inputType.startsWith("insertFromPaste")) {
        if (collectPasteFiles(e.dataTransfer || {}).length) return;
        e.preventDefault();
        const text = e.data || e.dataTransfer?.getData?.("text/plain") || "";
        if (text) this._insertText(text);
      }
    }

    _onPaste(e) {
      if (this.handlePaste(e.clipboardData)) {
        e.preventDefault();
      }
    }

    _syncCaretFromSelection() {
      if (this._rendering || this._composing) return;
      const sel = window.getSelection();
      if (!sel?.isCollapsed) return;
      const idx = this._readCaretFromDom();
      if (idx != null) this.caret = idx;
    }

    _readCaretFromDom() {
      const sel = window.getSelection();
      if (!sel?.rangeCount || !this.root.contains(sel.anchorNode)) return null;

      const expandedLabel = sel.anchorNode?.parentElement?.closest?.(
        ".composer-inline-chip.show-full-path .composer-inline-label"
      );
      if (expandedLabel) return null;

      const textRun = (
        sel.anchorNode.nodeType === Node.TEXT_NODE
          ? sel.anchorNode.parentElement
          : sel.anchorNode
      )?.closest?.(".composer-text-run");

      if (textRun) {
        const blockId = textRun.dataset.blockId;
        let offset = 0;
        if (sel.anchorNode.nodeType === Node.TEXT_NODE && textRun.contains(sel.anchorNode)) {
          offset = domOffsetToLogical(sel.anchorNode, sel.anchorOffset);
        } else if (textRun.firstChild?.nodeType === Node.TEXT_NODE) {
          offset = stripZwsp(textRun.firstChild.textContent).length;
        }
        return this._posToIndex({ kind: "text", blockId, offset });
      }

      const chip = (
        sel.anchorNode.nodeType === Node.ELEMENT_NODE
          ? sel.anchorNode
          : sel.anchorNode?.parentElement
      )?.closest?.(".composer-inline-chip:not(.show-full-path)");

      if (chip) {
        const rect = chip.getBoundingClientRect();
        const range = sel.getRangeAt(0);
        const rects = range.getClientRects();
        const x = rects.length ? rects[0].left : rect.left;
        const side = x > rect.left + rect.width / 2 ? "after" : "before";
        return this._posToIndex({ kind: "attach", blockId: chip.dataset.blockId, side });
      }

      return null;
    }

    _selectionToPos() {
      return null;
    }

    _applyCaret() {
      const pos = this._indexToPos(this.caret);
      const sel = window.getSelection();
      const range = document.createRange();
      const node = this._domForPos(pos);
      if (!node) {
        const last = this.root.querySelector(".composer-text-run:last-child");
        if (!last?.firstChild) return;
        const textNode = last.firstChild;
        const offset = Math.min(
          this.caret,
          stripZwsp(textNode.textContent).length
        );
        range.setStart(textNode, logicalOffsetToDom(textNode, offset));
        range.collapse(true);
        sel.removeAllRanges();
        sel.addRange(range);
        return;
      }
      if (node.kind === "text") {
        range.setStart(node.el, logicalOffsetToDom(node.el, node.offset));
      } else if (node.side === "before") {
        range.setStartBefore(node.el);
      } else {
        range.setStartAfter(node.el);
      }
      range.collapse(true);
      sel.removeAllRanges();
      sel.addRange(range);
    }

    _domForPos(pos) {
      if (pos.kind === "text") {
        const span = this.root.querySelector(`[data-block-id="${pos.blockId}"]`);
        if (!span?.firstChild) return null;
        const textNode = span.firstChild;
        const offset = Math.min(pos.offset, stripZwsp(textNode.textContent).length);
        return { kind: "text", el: textNode, offset };
      }
      if (pos.kind === "attach") {
        const chip = this.root.querySelector(`.composer-inline-chip[data-block-id="${pos.blockId}"]`);
        if (!chip) return null;
        return { kind: "attach", el: chip, side: pos.side };
      }
      const last = this.root.querySelector(".composer-text-run:last-child");
      if (last?.firstChild) {
        const tn = last.firstChild;
        return { kind: "text", el: tn, offset: tn.textContent?.length || 0 };
      }
      return null;
    }

    _render() {
      if (!this.root || this._composing) return;
      this._rendering = true;
      this.root.innerHTML = "";

      for (const block of this.blocks) {
        if (block.type === "text") {
          const span = document.createElement("span");
          span.className = "composer-text-run";
          span.dataset.blockId = block.id;
          span.appendChild(document.createTextNode(domTextForBlock(block.value)));
          this.root.appendChild(span);
          continue;
        }
        this.root.appendChild(this._buildChipEl(block));
      }

      if (!this._hasContent()) {
        if (this.blocks.length !== 1 || this.blocks[0].type !== "text") {
          this.blocks = [textBlock()];
        } else {
          this.blocks[0].value = "";
        }
        this.caret = 0;
      }

      this._rendering = false;
      this.root.classList.toggle("is-empty", !this._hasContent());
      this.root.classList.toggle("is-composing", this._composing);
      requestAnimationFrame(() => {
        if (!this._composing) this._applyCaret();
      });
    }

    _buildChipEl(block) {
      const chip = document.createElement("span");
      chip.className = "composer-inline-chip";
      chip.contentEditable = "false";
      chip.dataset.blockId = block.id;
      chip.dataset.path = block.markerPath || block.path || block.localId || "";
      chip.dataset.kind = block.kind || "file";
      chip.dataset.name = block.name || formatLabel(block);

      if (block.pending) chip.classList.add("pending");

      if (block.previewUrl) {
        const img = document.createElement("img");
        img.className = "composer-inline-thumb";
        img.src = block.previewUrl;
        img.alt = "";
        chip.appendChild(img);
      }

      const label = document.createElement("span");
      label.className = "composer-inline-label";
      const shortLabel = block.pending
        ? `${block.name || "图片"}（发送时上传）`
        : formatLabel(block);
      const fullPath = block.path || "";
      label.textContent = shortLabel;
      label.title = fullPath ? `${fullPath}\n（点击显示完整路径）` : shortLabel;

      if (fullPath && !block.pending) {
        label.addEventListener("click", (e) => {
          e.stopPropagation();
          const expanded = chip.classList.toggle("show-full-path");
          label.textContent = expanded ? fullPath : shortLabel;
          if (!expanded) {
            this.caret = this._posToIndex({ kind: "attach", blockId: block.id, side: "after" });
            this._applyCaret();
          }
        });
      }
      chip.appendChild(label);

      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = "×";
      btn.title = "移除";
      btn.addEventListener("mousedown", (e) => e.preventDefault());
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        this.caret = this._posToIndex({ kind: "attach", blockId: block.id, side: "before" });
        this._removeBlock(block.id);
      });
      chip.appendChild(btn);

      return chip;
    }
  }

  global.AgentComposer = AgentComposer;
  global.collectPasteFiles = collectPasteFiles;
})(window);
