# korprov monitory

**DO NOT BE AN ASSHOLE**
Keep your request rates to be polite. No point in slamming the REST endpoints and causing problems.

Polls the Trafikverket booking API for available körprov slots and sends a Telegram alert when one appears. Currently configured for **MC A (motorcycle driving test)** but can be used for any exam type — see [Other exam types](#other-exam-types) below.

## Setup

```bash
uv sync
uv run playwright install firefox
```

## Configuration

Edit the constants at the top of `korprov_monitor.py`:

| Variable | Description |
|---|---|
| `PERSONNUMMER` | Your Swedish personnummer (`YYYYMMDD-XXXX`) |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token — see below |
| `TELEGRAM_CHAT_ID` | Your Telegram chat ID — see below |
| `SEARCH_UNTIL_DATE` | Only alert on slots on or before this date (`YYYY-MM-DD`) |
| `CHECK_INTERVAL_SECONDS` | How often to poll (default: 60s) |
| `LOCATION_BATCHES` | Which test centres to monitor — uncomment to add more |

## Telegram setup

1. Message [@BotFather](https://t.me/BotFather) on Telegram and send `/newbot`
2. Copy the token it gives you into `TELEGRAM_BOT_TOKEN`
3. Send any message to your new bot, then run:

   ```bash
   curl "https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates"
   ```

4. Copy the `id` value from `result[0].message.from` into `TELEGRAM_CHAT_ID`

If `TELEGRAM_BOT_TOKEN` is left empty the script still runs and prints alert previews to stdout instead.

## Running

```bash
uv run python korprov_monitor.py
```

On first run (and whenever the session expires) the script opens a BankID QR code. On WSL2 the QR is saved to `/tmp/bankid_qr.png` and opened automatically with `explorer.exe` — scan it with the BankID app quickly as it rotates every second.

Once logged in the script checks all configured locations every `CHECK_INTERVAL_SECONDS` and sends a Telegram message if any slot is found before `SEARCH_UNTIL_DATE`.

## Adding locations

Location IDs are listed in comments in `LOCATION_BATCHES`. The API accepts one primary location plus up to three nearby locations per request — each tuple is one API call:

```python
(primary_id, "Primary Name", [nearby_id_1, nearby_id_2], ["Nearby 1", "Nearby 2"])
```

## Other exam types

The script works for any Trafikverket exam type — car, truck, taxi, etc. The three IDs at the top of `korprov_monitor.py` control which exam is searched:

```python
LICENCE_ID = 4           # MC A (full motorcycle licence)
EXAMINATION_TYPE_ID = 10  # Körprov (driving test)
VEHICLE_TYPE_ID = 1       # Motorcycle
```

To find the correct IDs for a different exam type:

1. Log in to [fp.trafikverket.se/Boka](https://fp.trafikverket.se/Boka/)
2. Open DevTools → Network tab
3. Search for the exam type you want
4. Find the POST request to `occasion-bundles` and inspect the request params
