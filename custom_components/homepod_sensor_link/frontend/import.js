class HomePodSensorLinkImport extends HTMLElement {
  connectedCallback() {
    if (this.shadowRoot) return;
    const root = this.attachShadow({mode: "open"});
    root.innerHTML = `<style>:host{display:block;padding:24px;max-width:700px;font-family:system-ui}label{display:block;margin:20px 0}textarea{width:100%;min-height:100px}button{padding:12px}p{line-height:1.5}</style>
      <h1>HomePod Sensor Link credentials</h1>
      <p>Select the exported HomeKit credential CSV or JSON file. Only the selected export is sent to this Home Assistant instance. Existing Apple Home pairings are preserved.</p>
      <label>Credential export <input type="file" accept=".json,.csv,application/json,text/csv"></label>
      <details><summary>Or enter controller credentials</summary>
      <label>Controller identifier <input id="controller" autocomplete="off" spellcheck="false"></label>
      <label>Controller private key (64 hexadecimal characters) <input id="seed" type="password" autocomplete="off" spellcheck="false"></label>
      <label>Accessory identifier (required for new sensor access) <input id="accessory" autocomplete="off" spellcheck="false"></label>
      <label>Pinned accessory public key <input id="pin" autocomplete="off" spellcheck="false"></label>
      <label>Or paste a complete JSON credential record <input id="record" type="password" autocomplete="off" spellcheck="false"></label></details>
      <button>Import credentials</button><p id="status" role="status"></p>`;
    root.querySelector("button").addEventListener("click", () => this.upload());
  }
  async upload() {
    const root = this.shadowRoot;
    const button = root.querySelector("button");
    const status = root.querySelector("#status");
    const file = root.querySelector("input[type=file]");
    const record = root.querySelector("#record");
    button.disabled = true;
    try {
      const params = new URLSearchParams(location.hash.slice(1));
      const flow = params.get("flow"); const nonce = params.get("nonce");
      if (!flow || !nonce) throw Error();
      const selected = file.files[0];
      if (selected && selected.size > 131072) throw Error();
      let body = selected ? await selected.text() : record.value;
      if (!body) {
        const controller = root.querySelector("#controller").value;
        const seed = root.querySelector("#seed").value;
        const accessory = root.querySelector("#accessory").value;
        const pin = root.querySelector("#pin").value;
        body = JSON.stringify({controllers: [{iOSPairingId: controller, iOSDeviceLTSK: seed}],
          accessories: accessory && pin ? [{AccessoryPairingID: accessory, AccessoryLTPK: pin}] : []});
      }
      if (new TextEncoder().encode(body).length > 131072) throw Error();
      const response = await this.hass.fetchWithAuth(`/api/homepod_sensor_link/import/${encodeURIComponent(flow)}`, {
        method: "POST", headers: {"Content-Type": "application/octet-stream", "X-HomePod-Import": nonce}, body
      });
      if (!response.ok) throw Error();
      status.textContent = "Credentials imported. Return to the setup tab and choose Continue. Nothing has been paired by this import.";
      history.replaceState(null, "", location.pathname);
    } catch (_) {
      status.textContent = "Import refused. Check the selected HAP export, local connection, administrator account and setup session. Return to setup to start a new import if it expired.";
      button.disabled = false;
    } finally { file.value = ""; for (const input of root.querySelectorAll("input:not([type=file])")) input.value = ""; }
  }
}
customElements.define("homepod-sensor-link-import", HomePodSensorLinkImport);
