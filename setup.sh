#!/data/data/com.termux/files/usr/bin/bash
#
# Tuntivelho Setup Script for Termux
# ====================================
# Run this in Termux to set up automatic punch-in/out.
#
# Usage:
#   bash setup.sh
#

set -e

echo "==================================="
echo "  Tuntivelho Setup"
echo "==================================="
echo ""

# 1. Check if Python is installed
if ! command -v python &> /dev/null; then
    echo "[1/4] Installing Python..."
    pkg install -y python
else
    echo "[1/4] Python already installed ✓"
fi

# 2. Test login
echo ""
echo "[2/4] Enter your Tuntivelho credentials:"
read -p "  Username (email): " TW_USER
read -sp "  Password: " TW_PASS
echo ""

echo ""
echo "Testing login..."
RESULT=$(python "$(dirname "$0")/tuntiwelho_api.py" --username "$TW_USER" --password "$TW_PASS" --action test_login 2>&1)
echo "$RESULT"

if echo "$RESULT" | grep -q "Login Test Passed"; then
    echo "✓ Login works!"
else
    echo "✗ Login failed. Check your credentials and try again."
    exit 1
fi

# 3. Store credentials securely
echo ""
echo "[3/4] Storing credentials..."
CRED_DIR="$HOME/.config/tuntivelho"
CRED_FILE="$CRED_DIR/credentials"
mkdir -p "$CRED_DIR"
cat > "$CRED_FILE" <<EOF
# Tuntivelho credentials — DO NOT SHARE
TW_USER="$TW_USER"
TW_PASS="$TW_PASS"
EOF
chmod 700 "$CRED_DIR"
chmod 600 "$CRED_FILE"
echo "✓ Credentials saved to $CRED_FILE (mode 600)"

# 4. Create Tasker scripts (credentials sourced at runtime)
echo ""
echo "[4/5] Creating Tasker scripts..."
mkdir -p ~/.termux/tasker/

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

cat > ~/.termux/tasker/punch_in.sh << 'SCRIPT_EOF'
#!/data/data/com.termux/files/usr/bin/bash

LOGFILE="$HOME/tuntivelho.log"
CRED_FILE="$HOME/.config/tuntivelho/credentials"

if [ ! -f "$CRED_FILE" ]; then
    echo "ERROR: Credentials file not found: $CRED_FILE" >&2
    echo "Run setup.sh again to create it." >&2
    exit 1
fi
source "$CRED_FILE"
export TW_USER TW_PASS

echo "==== $(date) PUNCH_IN ====" >> "$LOGFILE"

SCRIPT_EOF
# Append the python command with the resolved script directory
cat >> ~/.termux/tasker/punch_in.sh <<EOF
python "$SCRIPT_DIR/tuntiwelho_api.py" \\
  --action punch_in \\
  "\$@" 2>&1 | tee -a "\$LOGFILE"

echo "" >> "\$LOGFILE"
EOF

cat > ~/.termux/tasker/punch_out.sh << 'SCRIPT_EOF'
#!/data/data/com.termux/files/usr/bin/bash

LOGFILE="$HOME/tuntivelho.log"
CRED_FILE="$HOME/.config/tuntivelho/credentials"

if [ ! -f "$CRED_FILE" ]; then
    echo "ERROR: Credentials file not found: $CRED_FILE" >&2
    echo "Run setup.sh again to create it." >&2
    exit 1
fi
source "$CRED_FILE"
export TW_USER TW_PASS

echo "==== $(date) PUNCH_OUT ====" >> "$LOGFILE"

SCRIPT_EOF
# Append the python command with the resolved script directory
cat >> ~/.termux/tasker/punch_out.sh <<EOF
python "$SCRIPT_DIR/tuntiwelho_api.py" \\
  --action punch_out \\
  "\$@" 2>&1 | tee -a "\$LOGFILE"

echo "" >> "\$LOGFILE"
EOF

chmod +x ~/.termux/tasker/punch_in.sh
chmod +x ~/.termux/tasker/punch_out.sh

echo "✓ Created ~/.termux/tasker/punch_in.sh"
echo "✓ Created ~/.termux/tasker/punch_out.sh"

# 5. Allow Termux:Tasker access (idempotent)
echo ""
echo "[5/5] Setting Termux properties..."
mkdir -p ~/.termux/
if ! grep -q 'allow-external-apps=true' ~/.termux/termux.properties 2>/dev/null; then
    echo "allow-external-apps=true" >> ~/.termux/termux.properties
fi
echo "✓ Enabled external app access"

echo ""
echo "==================================="
echo "  Setup Complete! ✓"
echo "==================================="
echo ""
echo "Next steps:"
echo "  1. Import Tuntiwelho.prj.xml into Tasker"
echo "  2. Install Termux:Tasker from F-Droid"
echo "  3. You're done!"
echo ""
echo "To test manually:"
echo "  bash ~/.termux/tasker/punch_in.sh"
echo "  bash ~/.termux/tasker/punch_out.sh"
echo ""
echo "Credentials are stored in: $CRED_FILE"
echo "To change password: nano $CRED_FILE"

