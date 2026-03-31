# Sonoff SPM Bridge for Home Assistant

A Home Assistant addon that bridges Sonoff SPM (Smart Stackable Power Meter) devices to Home Assistant via MQTT, enabling real-time energy monitoring through the Energy Dashboard.

## What it does

- Reads real-time power, voltage, and current from each outlet channel
- Tracks cumulative energy consumption (kWh) with persistence across restarts
- Controls outlet switches (on/off) from Home Assistant
- Auto-discovers all entities in Home Assistant via MQTT
- Each SPM-4Relay module exposes 4 channels, each with: power (W), voltage (V), current (A), energy (kWh), and a switch

## Prerequisites

- Home Assistant with the [Mosquitto broker](https://github.com/home-assistant/addons/tree/master/mosquitto) addon (or any MQTT broker)
- Sonoff SPM-Main with DIY mode enabled (HTTP API on port 8081)
- SPM and Home Assistant on the same network

## Step 1: Prepare your Sonoff SPM

The SPM must be in **DIY mode** so it exposes its local HTTP API on port 8081. Your Home Assistant instance must be able to reach the SPM over the network.

### Enable DIY mode

1. **Power on the SPM-Main** and make sure it's connected to your network
2. **Long-press the button on the SPM-Main for 5 seconds** until the SIGNAL indicator starts flashing — this puts it into pairing/DIY mode
3. **Connect to the SPM's WiFi AP** (if not already on your network) and configure your WiFi credentials via the DIY web page
4. **Verify DIY mode is active** — once connected to your WiFi, the HTTP API should be accessible on port 8081

For detailed instructions specific to your firmware version, see [Sonoff's official guide](https://sonoff.tech/product-review/product-insight/get-started-quicklynow-you-can-control-spm-units-via-http-api/).

### Find the SPM's IP address

Check your router's admin page for the SPM's IP address. It may appear as `ESP_XXXXXX` or show an Espressif MAC address. Consider assigning it a static IP / DHCP reservation so it doesn't change.

### Verify connectivity

Test that the API is reachable:

```bash
curl -X POST http://<SPM_IP>:8081/zeroconf/deviceid \
  -H "Content-Type: application/json" \
  -d '{"data": {}}'
```

You should get a JSON response with `"error": 0` and a `deviceid`. If you get a connection error, DIY mode is not active or the IP is wrong.

## Step 2: Install the MQTT broker

If you don't have an MQTT broker running:

1. Go to **Settings > Add-ons > Add-on Store** in Home Assistant
2. Search for **Mosquitto broker**
3. Click **Install**, then **Start**
4. Go to **Settings > Devices & Services > MQTT** and configure if prompted

The addon defaults to connecting to `core-mosquitto` (the built-in Mosquitto addon) with no authentication. If your Mosquitto uses a username/password, note them for Step 4.

## Step 3: Add the addon repository

1. Go to **Settings > Add-ons > Add-on Store**
2. Click the **three dots** menu (top right) and select **Repositories**
3. Add this repository URL:
   ```
   https://github.com/xinuc/sonoff-spm-bridge
   ```
4. Click **Add**, then close the dialog
5. The **Sonoff SPM Bridge** addon should now appear in the store — click it and click **Install**

## Step 4: Configure the addon

Go to the addon's **Configuration** tab and set:

| Setting | Required | Default | Description |
|---------|----------|---------|-------------|
| `spm_host` | Yes | *(empty)* | IP address of your SPM device (e.g., `192.168.1.100`) |
| `mqtt_host` | No | `core-mosquitto` | MQTT broker hostname. Leave default if using the Mosquitto addon |
| `mqtt_port` | No | `1883` | MQTT broker port |
| `mqtt_username` | No | *(empty)* | MQTT username (if your broker requires auth) |
| `mqtt_password` | No | *(empty)* | MQTT password |
| `webhook_port` | No | `8080` | Port the addon listens on for SPM data pushes |
| `poll_interval` | No | `60` | How often (seconds) to re-register webhooks with the SPM. Lower = more reliable data, higher = less network traffic. Range: 10-300 |
| `log_level` | No | `info` | Log verbosity: `debug`, `info`, `warning`, `error` |

At minimum, you only need to set **`spm_host`** to your SPM's IP address.

## Step 5: Start the addon

1. Click **Start**
2. Go to the **Log** tab to verify:
   - `Connected to MQTT broker` — MQTT is working
   - `Discovered SPM device: XXXXXXXXXX` — SPM is reachable
   - `Found N sub-device(s)` — sub-modules detected
   - `Webhook registered for ...` — data push is active
   - `SPM Bridge running` — all good

If you see `SPM not reachable`, double-check the IP address and that DIY mode is enabled.

## Step 6: Verify in Home Assistant

After the addon starts:

1. Go to **Settings > Devices & Services > MQTT**
2. You should see a parent device **Sonoff SPM XXXXXXXXXX** (the SPM-Main)
3. Each SPM-4Relay module appears as a child device (**SPM Module XXXXXX**) with 4 channels
4. Each channel has 5 entities:
   - **Power** (W) — real-time power consumption
   - **Voltage** (V) — line voltage
   - **Current** (A) — current draw
   - **Energy** (kWh) — cumulative energy (compatible with Energy Dashboard)
   - **Switch** — on/off control

### Add to the Energy Dashboard

1. Go to **Settings > Dashboards > Energy**
2. Under **Electricity grid > Consumption**, click **Add consumption**
3. Select the **Energy** entities (e.g., `SPM XXXXXXXX Ch0 Energy`)
4. Save — energy data will start appearing within the hour

## How it works

```
SPM (port 8081)                    Addon (port 8080)              Home Assistant
     │                                  │                              │
     │◄── register webhook (per outlet) │                              │
     │                                  │                              │
     ├── push data (on threshold) ────► │── convert units ──► MQTT ──► │
     │                                  │                              │
     │◄── re-register (keep-alive) ─────│                              │
     │                                  │                              │
     │◄── switch command ───────────────│ ◄── MQTT command ◄───────────│
```

The SPM pushes real-time data via webhooks whenever readings change (current >0.03A, voltage >5V, or power >2W change). The addon re-registers webhooks periodically to keep the monitoring session alive. There is no polling — all data arrives via push.

## Troubleshooting

### No entities appear in Home Assistant
- Check the addon log for errors
- Verify MQTT is connected: the log should show `Connected to MQTT broker`
- Verify the SPM is discovered: the log should show `Discovered SPM device`
- Check that the MQTT integration is set up in Home Assistant (**Settings > Devices & Services > MQTT**)

### Power/voltage/current values are not updating
- Check the addon log for `Webhook registered for ...` messages
- Make sure port 8080 is not blocked by a firewall between the SPM and Home Assistant
- Try lowering `poll_interval` to 30 for more frequent re-registration
- Set `log_level` to `debug` for detailed webhook traffic logs

### Switch control not working
- The addon log should show `Switch ... chN -> on/off` on success
- On failure, it logs `Switch ... FAILED` — check that the SPM is reachable
- Verify the SPM firmware supports the `/zeroconf/switches` endpoint

### Energy values reset to 0
- Energy totals are persisted in `/data/energy_state.json` inside the addon container
- Totals persist across addon restarts but reset if the addon is **uninstalled and reinstalled**
- If there's a gap of more than 10 minutes between readings, the gap is skipped (no phantom energy accumulation)

### SPM not reachable
- Confirm the IP address is correct and the SPM is on the same network
- Test with: `curl -X POST http://<IP>:8081/zeroconf/deviceid -d '{"data": {}}'`
- The SPM may have changed IP — check your router and consider a static IP / DHCP reservation

## Known limitations

- **Threshold-based updates**: The SPM only pushes data when readings change beyond thresholds (current >0.03A, voltage >5V, power >2W). Channels with a perfectly stable load may not send frequent updates. Channels with zero load will not send updates at all.
- **Out-of-band switch changes**: If a switch is toggled via the physical button on the SPM or through the eWeLink app, this addon won't detect the change until the next data push. The addon tracks switch state from its own commands and the initial state query on startup.
- **DIY mode required**: The SPM must remain in DIY mode for the HTTP API to be accessible. Switching back to eWeLink cloud mode (via the eWeLink app or `/zeroconf/ops_mode`) will disable the local API and break connectivity.
- **Single SPM-Main**: This addon connects to one SPM-Main device. If you have multiple SPM-Main units, you would need multiple instances of the addon.

## License

MIT
