# CEC Remote API

A small FastAPI service that keeps `cec-client` running in a dedicated worker
thread. HTTP handlers validate and enqueue commands, so concurrent requests do
not write to the subprocess directly.

## Raspberry Pi setup

Install the system dependency and `uv`:

```sh
sudo apt update
sudo apt install cec-utils curl
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Open a new shell so `uv` is on `PATH`, then create the virtual environment and
install the locked Python dependencies from the project directory:

```sh
uv sync --frozen
```

Start one API process (do not use multiple Uvicorn workers, because each would
start its own `cec-client`):

```sh
uv run --frozen uvicorn main:app --host 0.0.0.0 --port 8000
```

Interactive API documentation is available at `http://<pi-address>:8000/docs`.
The browser-based remote is available at `http://<pi-address>:8000/`.

The remote is also an installable Progressive Web App. Open it in a supported
browser and choose **Install app** or **Add to Home Screen**. Service workers
require a secure context: `localhost` works for local development, while access
from another device normally needs HTTPS. The service worker caches only the
app shell; control commands are always sent to the API and are never cached.

The original grey theme is used by default. Open `/?theme=green` to use or
install the olive-green variant; its browser theme, manifest, and app icons are
selected along with the interface colors.

## systemd service

The included unit is a template whose instance name is the Linux user running
the API. It assumes the project is installed at `/home/<user>/cecremote` and its
uv-managed virtual environment is `/home/<user>/cecremote/.venv`. If your path
differs, edit `WorkingDirectory` and `ExecStart` in `cec-remote@.service` first.

From the project directory on the Raspberry Pi, install and start the service:

```sh
sudo cp 'cec-remote@.service' /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now "cec-remote@${USER}.service"
```

Check its status and follow its logs with:

```sh
systemctl status "cec-remote@${USER}.service"
journalctl -u "cec-remote@${USER}.service" -f
```

After changing Python code, restart it with:

```sh
sudo systemctl restart "cec-remote@${USER}.service"
```

After changing dependencies in `pyproject.toml`, update the lockfile and sync
the environment before restarting:

```sh
uv lock
uv sync --frozen
sudo systemctl restart "cec-remote@${USER}.service"
```

The unit adds the service account to the `video` supplementary group, which is
normally needed to access `/dev/cec0`. Confirm the adapter and its permissions
with `ls -l /dev/cec*` if `cec-client` cannot open the device.

## Requests

```sh
curl -X POST http://localhost:8000/power/on
curl -X POST http://localhost:8000/power/off
curl -X POST http://localhost:8000/volume/up
curl -X POST http://localhost:8000/volume/down
curl -X POST http://localhost:8000/source \
  -H 'Content-Type: application/json' \
  -d '{"physical_address":"2.0.0.0"}'
```

The source endpoint takes a CEC physical address, not a logical address. Common
direct TV inputs are `1.0.0.0`, `2.0.0.0`, and so on. Devices connected through
an AVR or switch can have addresses such as `2.1.0.0`. Source switching varies
somewhat by TV manufacturer and may require enabling HDMI-CEC in the TV menu.

Every successful command request returns HTTP 202 because it has been queued,
not necessarily completed by the television. If `cec-client` exits or is not
yet available, the worker keeps the command queued and retries after restarting
the subprocess.

## Configuration

Source buttons are configured in `sources.json`. Each key is a CEC physical
address and each value is the button label; entries are displayed in file order:

```json
{
  "1.0.0.0": "HDMI 1",
  "2.0.0.0": "Game console"
}
```

Edit this file to match the devices connected to your display. Set
`CEC_SOURCES_FILE` to use a configuration file in another location.

Environment variables:

- `CEC_CLIENT`: executable path (default: `cec-client`)
- `CEC_DEVICE_TYPE`: value passed to `cec-client -t` (default: `p`, playback)
- `CEC_RESTART_DELAY`: seconds between start/send retries (default: `2`)
- `CEC_SOURCES_FILE`: source-name JSON file (default: `sources.json` beside `main.py`)
- `LOG_LEVEL`: Python log level (default: `INFO`)

For LAN use, put the API behind firewall rules or an authenticated reverse
proxy; the service intentionally has no built-in authentication.
