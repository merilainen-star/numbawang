# Tuntivelho Auto-Puncher

Automates punch-in/out for the [Tuntivelho](https://app.tuntivelho.com) (Finago Mobiili) timecard app, running on Android via **Termux + Tasker**.

> Backend note (Mar 2026): Tuntivelho moved the mobile GraphQL endpoint from
> `/tvv-mobile/...` to `/mobiili/...`. This script now auto-tries the new
> endpoint first and falls back to the old one for compatibility.

## Quick Setup (Android)

1. Install from F-Droid: **Termux**, **Termux:Tasker**
2. Install from Play Store: **Tasker**
3. Open Termux and run:

```bash
pkg install git python -y
git clone https://github.com/merilainen-star/numbawang ~/tuntivelho
cd ~/tuntivelho && bash setup.sh
```

4. Import `Tuntiwelho.prj.xml` into Tasker

## How It Works

- **07:30 Mon-Fri** → notification: "Töihin?" with **Sisään** button
- **16:00 Mon-Fri** → notification: "Kotiin?" with **Ulos** button
- Tapping the button runs a Python script that calls the Tuntivelho GraphQL API

## Files

| File | Description |
|------|-------------|
| `tuntiwelho_api.py` | Core Python script (stdlib only, no pip needed) |
| `setup.sh` | Interactive Termux setup wizard |
| `Tuntiwelho.prj.xml` | Tasker project (import into Tasker) |

## Manual Usage

Set your credentials as environment variables (recommended):
```bash
export TW_USER="you@email.com"
export TW_PASS="your_password"

python tuntiwelho_api.py --action punch_in
python tuntiwelho_api.py --action punch_out
python tuntiwelho_api.py --action test_login
```

Optional: force a specific API endpoint (normally not needed):
```bash
export TW_API_URL="https://app.tuntivelho.com/mobiili/backend/public/graphql"
```

Or pass them inline (less secure — visible in `ps` output):
```bash
python tuntiwelho_api.py --username "you@email.com" --password "pass" --action punch_in
```

Add `--dry-run` to simulate without actually punching.

## Tasker Compatibility

This endpoint fix does **not** change the Tasker output format. The script still
emits the same machine-readable keys:

- `TV_STATUS=...`
- `TV_RESULT=STATUS|ACTION|EPOCH|message`

Your existing Tasker profile/project logic should continue to work unchanged.

## Troubleshooting

- `HTTP Error 404 LOGIN`: backend endpoint has likely changed again. First pull
  latest changes, then test with:
  ```bash
  python tuntiwelho_api.py --action test_login
  ```
- If needed, set `TW_API_URL` to a known working endpoint and retry.
