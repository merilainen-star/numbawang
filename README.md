# Tuntivelho Auto-Puncher

Automates punch-in/out for the [Tuntivelho](https://app.tuntivelho.com) (Finago Mobiili) timecard app, running on Android via **Termux + Tasker**.

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

```bash
python tuntiwelho_api.py --username "you@email.com" --password "pass" --action punch_in
python tuntiwelho_api.py --username "you@email.com" --password "pass" --action punch_out
python tuntiwelho_api.py --username "you@email.com" --password "pass" --action test_login
```

Add `--dry-run` to simulate without actually punching.
