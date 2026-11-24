# AC Monitor

Monitor a Beko / Arçelik air conditioner and run it on a **time-of-use schedule**,
without depending on the HomeWhiz app. Runs 24/7 on a Raspberry Pi (or any small
Linux box) and gives you a web dashboard on your LAN.

- **Live state:** power, mode, set point, fan speed and room temperature from the AC itself.
- **Time-of-use windows:** e.g. weekdays 17:30–22:00 Cool 24°, nightly 23:00–06:30 Cool 26°.
  The AC is switched on at the start of a window and off at the end.
- **Auto-off timer** for manual use ("cool for 90 minutes").
- **History:** room-temperature chart with shaded running periods, daily runtime and estimated kWh,
  plus an event log. Changes made with the physical remote are detected and logged too.

## How it talks to the AC

| Driver | Needs | Reads real state? |
|---|---|---|
| `homewhiz` (recommended) | The AC already on Wi-Fi **and linked to your HomeWhiz account** (it stays linked even if the app misbehaves) | Yes |
| `broadlink` | A Broadlink RM4 mini / RM4 Pro (~€25) pointed at the AC | No: it remembers what it last sent. The RM4 Pro / RM4 mini with sensor cable logs room temperature |
| `mock` | Nothing | Fake data, for trying the dashboard |

The `homewhiz` driver uses the same cloud as the app: it logs in with your HomeWhiz
account, downloads your model's configuration (which byte holds which setting), then
subscribes to the AC's AWS IoT device shadow and publishes write commands. The protocol
details come from the community
[home-assistant-HomeWhiz](https://github.com/home-assistant-HomeWhiz/home-assistant-HomeWhiz)
integration, the source used here for how it works.

## Install

Everything is pure Python (its only dependency is `paho-mqtt`), so it runs on any Raspberry Pi
(Zero W included, 32- or 64-bit OS), any Linux box, or an Android phone (see below).
Python 3.11 or newer is required.

### Android phone (Termux), to try it out or as a stop-gap server

Install **Termux** from F-Droid (the Play Store version is outdated). Then:

```bash
pkg update && pkg install python
termux-setup-storage                       # lets Termux read your Downloads folder
mkdir -p ~/ac && cd ~/ac
python -m zipfile -e ~/storage/downloads/acmonitor.zip .
pip install -e .
cp config.example.toml config.toml && nano config.toml
```

Run the checks below (steps 1–2). To keep the service running, use `termux-wake-lock` then `acmonitor serve`,
and open `http://localhost:8080` in the phone's browser (or `http://<phone-ip>:8080` from another device).
In Android settings, exclude Termux from battery optimisation. A phone is fine to start with,
but for reliable 24/7 schedules move to a Raspberry Pi later.

### Raspberry Pi

```bash
sudo apt install -y python3-venv git
git clone <this repo> ~/HomeMonitoring && cd ~/HomeMonitoring
python3 -m venv .venv && .venv/bin/pip install -e .
cp config.example.toml config.toml && chmod 600 config.toml
nano config.toml           # username/password, timezone, rated_power_kw
```

### 1. Check the AC is still in your account

```bash
.venv/bin/acmonitor homewhiz-appliances
```

You should see a line marked `<- Wi-Fi air conditioner`. If there are several, put the
right `applianceId` in `homewhiz.appliance_id`.

### 2. Read its live state

```bash
.venv/bin/acmonitor status
```

Compare the printed `power`, `mode`, `target_temp` and `room_temp` with the AC's display.
Then change something with the remote and run it again. If both match, monitoring works.

### 3. Run it as a service

```bash
sudo cp deploy/acmonitor.service /etc/systemd/system/   # edit User/paths if not "pi"
sudo systemctl daemon-reload && sudo systemctl enable --now acmonitor
```

Open `http://<pi-address>:8080` on your phone and add it to the home screen.

## Using it

1. **Presets:** named settings such as `cool24` = Cool, 24 °C, fan auto. Create them in
   the dashboard or with `acmonitor preset cool24 --label "Cool 24°" --mode cool --temp 24 --fan auto`.
   With `homewhiz` you don't need an "off" preset.
2. **Time-of-use windows:** pick days, on time, off time and a preset.
   - At the **start** the preset is applied; at the **end** the AC is switched off.
   - Off earlier than on (e.g. 23:00 → 06:30) means the window runs past midnight.
   - Overlapping windows: the most recently started one wins; when it ends, the other resumes.
   - Switching the AC off by hand during a window is respected; it won't be turned back on until the next window.
   - If the Pi was down when a window started, it is applied when the service comes back
     (if the window is still running).
3. **Control card:** one-tap presets, any mode/temperature/fan, and an optional auto-off.

Energy figures are **estimates** (`runtime × monitor.rated_power_kw`). Check the input
power on your unit's rating label and use roughly 60–70 % of it for an inverter AC. If
your model reports instantaneous consumption, the dashboard shows it as "Reported draw".

## IR fallback (if the AC isn't in your account)

Set `backend = "broadlink"`, then for each preset:

```bash
.venv/bin/acmonitor discover                     # find the blaster
.venv/bin/acmonitor learn off --off
.venv/bin/acmonitor learn cool24 --label "Cool 24°" --mode cool --temp 24
```

Set the Beko remote to the wanted mode/temperature **before** pressing its button. The
remote sends its whole state with every press, so one learned code is one complete preset.
Back up codes with `acmonitor export-presets > presets.json`.

## If HomeWhiz pairing keeps failing

You only need it once, if the AC ever drops out of the account. Common causes:
the phone on 5 GHz or the router using band steering (the AC needs 2.4 GHz), WPA3-only
security, special characters in the SSID or password, a guest network, or a VPN or
private DNS on the phone during pairing.

## Security

- The dashboard has **no login**: keep it on your LAN or behind a VPN (WireGuard/Tailscale).
  Never port-forward it.
- `config.toml` holds your HomeWhiz password: `chmod 600`, or use the `HOMEWHIZ_PASSWORD`
  environment variable instead.

## Development

```bash
pip install -e '.[dev]'
pytest
acmonitor -c config.toml serve     # with backend = "mock" to try the UI without hardware
```

`acmonitor/drivers/homewhiz.py` is the cloud protocol, `controller.py` the scheduler and
state tracking, `web.py` + `static/index.html` the API and dashboard.
