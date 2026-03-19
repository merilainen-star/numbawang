# Release Notes

## 2026-03-19 - Login endpoint hotfix

### Fixed
- Fixed `HTTP Error 404 LOGIN` caused by backend endpoint change.
- Updated GraphQL primary endpoint to:
  - `https://app.tuntivelho.com/mobiili/backend/public/graphql`

### Improved
- Added endpoint fallback support:
  - Primary: `/mobiili/backend/public/graphql`
  - Fallback: `/tvv-mobile/backend/public/graphql`
- Added optional `TW_API_URL` environment variable override for quick recovery if endpoint changes again.

### Compatibility
- No Tasker project changes required.
- `TV_STATUS` and `TV_RESULT` output format stays unchanged.
- Existing Tasker parsing logic remains compatible.

### Validation
- `--action test_login` now succeeds again against live backend.
- `--dry-run` and real punch flow verified after the fix.

### Upgrade
In Termux:

```bash
cd ~/tuntivelho
git pull
python tuntiwelho_api.py --action test_login
```

