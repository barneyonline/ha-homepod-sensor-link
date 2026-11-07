import assert from "node:assert/strict";
import {test} from "node:test";

let Panel;
class Element {
  value = "";
  files = [];
  disabled = false;
  textContent = "";
  handlers = new Map();
  addEventListener(name, handler) { this.handlers.set(name, handler); }
}
class Root {
  innerHTML = "";
  elements = new Map(["button", "#status", "input[type=file]", "#record", "#controller", "#seed", "#accessory", "#pin"].map(name => [name, new Element()]));
  querySelector(name) { return this.elements.get(name); }
  querySelectorAll() { return ["#record", "#controller", "#seed", "#accessory", "#pin"].map(name => this.querySelector(name)); }
}
globalThis.HTMLElement = class {
  shadowRoot = null;
  attachShadow() { return this.shadowRoot = new Root(); }
};
globalThis.customElements = {define(name, constructor) {
  assert.equal(name, "homepod-sensor-link-import"); Panel = constructor;
}};
await import("../../custom_components/homepod_sensor_link/frontend/import.js");

function panel(hash = "#flow=disposable%2Fflow&nonce=synthetic-nonce") {
  globalThis.location = {hash, pathname: "/homepod-sensor-link-import"};
  const replaced = [];
  globalThis.history = {replaceState(...args) { replaced.push(args); }};
  const element = new Panel();
  element.connectedCallback();
  const root = element.shadowRoot;
  element.connectedCallback();
  assert.equal(element.shadowRoot, root);
  const calls = [];
  element.hass = {async fetchWithAuth(url, options) { calls.push({url, options}); return {ok: true}; }};
  return {element, root, calls, replaced};
}
function cleared(root) {
  assert.equal(root.querySelector("input[type=file]").value, "");
  for (const input of root.querySelectorAll()) assert.equal(input.value, "");
}

test("selected file is sent through HA authentication and all fields are cleared", async () => {
  const {element, root, calls, replaced} = panel();
  const payload = JSON.stringify({controllers: ["synthetic-test-only"]});
  root.querySelector("input[type=file]").files = [{size: payload.length, async text() { return payload; }}];
  root.querySelector("#seed").value = "ef".repeat(32);
  await root.querySelector("button").handlers.get("click")();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "/api/homepod_sensor_link/import/disposable%2Fflow");
  assert.equal(calls[0].options.body, payload);
  assert.equal(calls[0].options.headers["X-HomePod-Import"], "synthetic-nonce");
  assert.equal(calls[0].options.method, "POST");
  assert.equal(root.querySelector("button").disabled, true);
  assert.match(root.querySelector("#status").textContent, /Return to the setup tab/);
  assert.deepEqual(replaced, [[null, "", "/homepod-sensor-link-import"]]);
  cleared(root);
});

test("explicit fields support paired pins and controller-only native adoption", async () => {
  for (const [accessory, pin] of [["disposable-accessory", "ab".repeat(32)], ["", ""], ["accessory", ""]]) {
    const {element, root, calls} = panel();
    root.querySelector("#controller").value = "disposable-controller";
    root.querySelector("#seed").value = "ef".repeat(32);
    root.querySelector("#accessory").value = accessory;
    root.querySelector("#pin").value = pin;
    await element.upload();
    const body = JSON.parse(calls[0].options.body);
    assert.equal(body.controllers[0].iOSPairingId, "disposable-controller");
    assert.equal(body.accessories.length, accessory && pin ? 1 : 0);
    cleared(root);
  }
});

test("pasted record takes precedence over explicit fields", async () => {
  const {element, root, calls} = panel();
  root.querySelector("#record").value = "synthetic-complete-record";
  await element.upload();
  assert.equal(calls[0].options.body, "synthetic-complete-record");
  cleared(root);
});

test("missing session, oversized file and UTF-8 body are refused before upload", async () => {
  for (const scenario of ["no-flow", "no-nonce", "file", "utf8"]) {
    const {element, root, calls, replaced} = panel(scenario === "no-flow" ? "#nonce=n" : scenario === "no-nonce" ? "#flow=f" : undefined);
    root.querySelector("#seed").value = "ef".repeat(32);
    if (scenario === "file") root.querySelector("input[type=file]").files = [{size: 131073, text() { assert.fail("oversize file must not be read"); }}];
    if (scenario === "utf8") root.querySelector("#record").value = "🔒".repeat(40000);
    await element.upload();
    assert.equal(calls.length, 0);
    assert.equal(replaced.length, 0);
    assert.equal(root.querySelector("button").disabled, false);
    assert.match(root.querySelector("#status").textContent, /^Import refused/);
    cleared(root);
  }
});

test("server refusal and network errors reveal no raw messages and allow retry", async () => {
  for (const fail of [false, true]) {
    const {element, root, replaced} = panel();
    const privateValue = "synthetic-sensitive-input";
    root.querySelector("#record").value = privateValue;
    element.hass.fetchWithAuth = async () => { if (fail) throw Error(privateValue); return {ok: false}; };
    await element.upload();
    assert.equal(root.querySelector("button").disabled, false);
    assert.equal(replaced.length, 0);
    assert.doesNotMatch(root.querySelector("#status").textContent, /synthetic-sensitive-input/);
    cleared(root);
  }
});
