import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const source = readFileSync(new URL("../../../server/static/shared/image-input.js", import.meta.url), "utf8")
  .replace(/^import .*;$/gm, "");
const stubs = `const t = key => key; const basePath = ''; const createLogger = () => ({info() {}, warn() {}, error() {}});\n`;
const { createImageInput } = await import(`data:text/javascript;base64,${Buffer.from(stubs + source).toString("base64")}`);

test("a rejected attachment blocks text-only send once per rejection", () => {
  const element = { addEventListener() {}, classList: { add() {}, remove() {} } };
  const preview = { innerHTML: "", style: {}, querySelectorAll() { return []; } };
  const manager = createImageInput({ container: element, inputArea: element, previewContainer: preview });
  for (let attempt = 0; attempt < 2; attempt++) {
    manager.addFiles([{ name: "large.png", type: "image/png", size: 6 * 1024 * 1024 }]);
    assert.equal(manager.prepareForSubmit(), false);
    assert.match(preview.innerHTML, /chat.image_send_without_attachment/);
    assert.equal(manager.prepareForSubmit(), true);
    assert.equal(manager.prepareForSubmit(), true);
  }
});
