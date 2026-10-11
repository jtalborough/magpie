# Operations and hardware notes

Detailed setup for the LED cabinet, Raspberry Pi edge devices, voice, and the assistant backend.

### LED Controller

The LED strip is a WS2812-style one-way chain. It has no readable addresses.
To light one pixel, the controller sends a full-strip frame where every pixel is
off except the requested index.

Current hardware assumptions:

- Raspberry Pi 5
- GLEDOPTO WLED controller at `http://192.0.2.37`
- Strip connected to the controller's GPIO2 output
- 182 mapped WS2812 pixels
- Pi API proxies to WLED instead of bit-banging LED data directly

#### Local Pi Install

Copy or clone this repo onto the Pi, then run:

```bash
scripts/install-pi5-led-service.sh
```

The installer creates a virtualenv under `~/searchable-storage-led/.venv`,
copies `config/led-controller.json`, creates `~/searchable-storage-led/secrets.env`,
and starts a user systemd service. `secrets.env` is loaded by systemd and is the
right place for local API keys.

Check service health:

```bash
systemctl --user status searchable-storage-led.service
curl http://127.0.0.1:8765/health
curl http://127.0.0.1:8765/build
curl http://127.0.0.1:8765/assistant/status
```

#### Pi Deployments

`.github/workflows/deploy-pi.yml` deploys an explicitly selected revision to the
Pi. It runs on a GitHub self-hosted runner registered on the Pi with the labels
`self-hosted, searchable-storage-pi`. The runner connects outbound to GitHub, so
the Pi needs no inbound SSH.

Because this repository is public, the Pi workflows (`deploy-pi`,
`pi-diagnostics`, `pi-voice-test`) only accept `workflow_dispatch` from the
repository owner, run in the reviewer-protected `magpie-production`
environment, and never run for pull requests. Keep it that way: never add a
`pull_request` trigger to a job that targets a self-hosted runner.

Optional repository variable `PI_APP_DIR` sets the runtime install directory on
the Pi (default `~/searchable-storage-led`).

#### API Examples

Light the whole strip red:

```bash
curl -X POST http://127.0.0.1:8765/strip \
  -H 'content-type: application/json' \
  -d '{"color":[255,0,0],"brightness":0.4}'
```

Light pixel 48 and clear everything else:

```bash
curl -X POST http://127.0.0.1:8765/pixels/48 \
  -H 'content-type: application/json' \
  -d '{"color":[255,255,255],"brightness":0.4,"clear_others":true}'
```

Set multiple pixels in one frame:

```bash
curl -X POST http://127.0.0.1:8765/pixels \
  -H 'content-type: application/json' \
  -d '{"clear_others":true,"pixels":[[0,[255,0,0]],[181,[0,255,0]]]}'
```

Clear the strip:

```bash
curl -X POST http://127.0.0.1:8765/clear
```

The API has no authentication. Bind it only to a private network interface or
put it behind a trusted local controller.

### Inventory Prototype

The service also hosts a browser prototype at:

```text
http://pi5:8765/
```

Current layout assumptions live in `config/storage-layout.json`:

- Origin is bottom-right.
- LED wiring is serpentine, moving upward.
- `B001` is the first bottom-right bin.
- The upper strip starts at `B112` as LED `0`, then moves left to `B099` as LED `13`.
- 8 rows of 14 small bins.
- 3 rows of 4 medium bins.
- 2 rows of 2 large bins.
- Total physical bins: 128.

The prototype separates physical bins from LED implementation details. Bins can
own one or more LED indexes, so large bins flash all LEDs associated with that
container. The main workflow is the Assistant panel: type or speak a request
like "I need heat shrink" or "put these screws away", optionally attach a photo,
then confirm the suggested inventory update.

Speech input uses the browser's built-in Web Speech API when available. If the
browser does not support it, the Mic button is disabled and typed commands still
work. Speech recognition generally requires a secure browser context; use
`https://...` or access the service as `localhost` through an SSH tunnel when
testing from a laptop:

```bash
ssh -L 8765:127.0.0.1:8765 pi@pi5
```

Then open:

```text
http://127.0.0.1:8765/
```

### Pi screen, microphone, and voice

The cabinet screen runs the same browser prototype in a Chromium kiosk pointed
at the local Pi service:

```text
http://127.0.0.1:8765/
```

That local URL is important: browsers treat `localhost` as a secure context, so
the Mic button can request microphone access without adding TLS to the prototype
service. The kiosk launcher also passes Chromium
`--use-fake-ui-for-media-stream`, which grants the microphone prompt for the
dedicated cabinet browser profile instead of requiring a click after every restart.
For room-side hands-free use, prefer the Pi voice listener instead of browser
speech so Chromium does not hold the microphone open.

Install or restart the kiosk from the Pi checkout:

```bash
scripts/install-pi5-kiosk.sh
```

Install the voice listener from the deployed Pi app:

```bash
scripts/install-pi5-voice-service.sh
```

The listener records short clips from the reSpeaker input, waits for "Magpie",
sends the command to the local assistant API, and speaks the response. It uses
`OPENAI_API_KEY` from `~/searchable-storage-led/secrets.env` for speech-to-text
and text-to-speech. Current Pi defaults:

```bash
MAGPIE_VOICE_INPUT_DEVICE=plughw:2,0
MAGPIE_VOICE_OUTPUT_DEVICE=plughw:0,0
MAGPIE_WAKE_WORD=magpie
```

On the current Pi, `plughw:0,0` is the first HDMI audio output, which is the
best bet for speakers in the connected screen. The reSpeaker also exposes a USB
playback endpoint at `plughw:2,0`; use that as the output device only if the
board or attached audio hardware is actually audible.

Voice tuning works best one layer at a time. First test the routing without
recording audio:

```bash
searchable-storage-voice --transcript "Magpie find m four tea nuts" --json
```

Expected shape:

```json
{
  "command": "find m four tea nuts",
  "query": "M4 T-nuts",
  "route": "/assistant/find"
}
```

Then test one real cabinet capture:

```bash
searchable-storage-voice --once --json
```

To include the backend result without speaking back:

```bash
searchable-storage-voice --once --call-backend --json
```

To compare speech models against the same recorded clip:

```bash
searchable-storage-voice --once --compare-stt-model gpt-4o-mini-transcribe --json
```

The daemon logs every clip to the local API. Review recent attempts from any
browser or terminal:

```bash
curl http://127.0.0.1:8765/voice/recent?limit=10
```

Tune `MAGPIE_VOICE_AUDIO_THRESHOLD` if quiet-room clips are transcribed or real
speech is skipped. The diagnostic output includes `rms`, `transcript`, `command`,
`route`, `query`, backend `response`, and the `speech` Magpie would say.

Useful screen commands on the Pi:

```bash
systemctl --user status searchable-storage-kiosk.service
journalctl --user -u searchable-storage-kiosk.service -f
systemctl --user restart searchable-storage-kiosk.service
systemctl --user status searchable-storage-voice.service
journalctl --user -u searchable-storage-voice.service -f
```

To install through GitHub Actions, run **Deploy to Pi** manually and enable
`install_kiosk` and/or `install_voice`. Normal pushes still deploy only the
backend service so a broken desktop session, unplugged screen, or unavailable
audio device does not block API updates.

Check what the Pi can see:

```bash
scripts/check-pi-peripherals.sh
```

That prints app health, display session, Chromium install, ALSA microphone
devices, a two-second microphone capture test, and USB devices. If `arecord` is
missing, install the ALSA tools on the Pi:

```bash
sudo apt-get update
sudo apt-get install -y alsa-utils
```

#### Assistant Backend

The assistant backend is controlled by `config/led-controller.json`:

```json
"assistant_backend": "auto",
"openai_model": "gpt-5.5"
```

Modes:

- `auto`: use OpenAI when `OPENAI_API_KEY` is set, otherwise fall back to the
  deterministic local assistant.
- `deterministic`: never call OpenAI.
- `openai`: require OpenAI and return an error if the API key or request fails.

Set the API key in the service environment, not in the config file:

```bash
nano ~/searchable-storage-led/secrets.env
```

Add:

```bash
OPENAI_API_KEY=sk-proj-...
```

Then restart the service:

```bash
systemctl --user restart searchable-storage-led.service
```

Check whether the app is ready to use OpenAI:

```bash
curl http://pi5:8765/assistant/status
```

`effective_backend` should be `openai` before testing model-based image or item
judgment. If it is `deterministic`, the app is working but using local fallback.
If it is `openai_unconfigured`, `assistant_backend` is set to `openai` but the
API key is missing.

OpenAI suggestions are treated as drafts. The application still validates bin
IDs, checks existing inventory, and waits for the user to save before mutating
inventory state.

Useful API calls:

```bash
## list layout
curl http://pi5:8765/layout

## flash a physical bin
curl -X POST http://pi5:8765/bins/B001/flash \
  -H 'content-type: application/json' \
  -d '{"color":[255,255,255],"brightness":1.0}'

## add or update inventory
curl -X POST http://pi5:8765/inventory \
  -H 'content-type: application/json' \
  -d '{"name":"M5 pan head screw","bin_id":"B001","quantity":12,"unit":"each","category":"fasteners","aliases":["5mm screw","panhead"]}'

## search inventory
curl 'http://pi5:8765/search?q=panhead%20screw'

## assistant command
curl -X POST http://pi5:8765/assistant/command \
  -H 'content-type: application/json' \
  -d '{"text":"put these allen keys away","photo_ids":[]}'
```
