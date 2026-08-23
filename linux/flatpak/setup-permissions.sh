#!/bin/bash
# setup-permissions.sh - Post-install permission setup for Open Voice Input Linux
# Optional read-only permission check for the compatibility Flatpak.

set -euo pipefail

echo "=== Open Voice Input Linux Permission Setup ==="
echo ""

if flatpak info com.doubao.Murmur >/dev/null 2>&1; then
    flatpak info --show-permissions com.doubao.Murmur
else
    echo "The compatibility Flatpak is not installed for this user."
fi

cat <<'EOF'

Inline IBus preedit does not require root access, membership in the input
group, a system-wide ydotool daemon, or Flatpak --device=all. This script no
longer grants those broad legacy permissions.

On X11, the optional final-paste fallback can use a user-installed xdotool.
It remains clipboard-only when the target window cannot be verified. The
recommended path is the focus-bound org.murmur.IME.Preedit1 engine.
EOF
