#!/bin/bash
# MicMix installer for macOS: one command, and nothing in it needs your password. In Terminal, paste:
#
#     curl -fsSL https://raw.githubusercontent.com/benjaminhoffman-bit/micmix/main/install.sh | bash
#
# or run `bash install.sh` in an extracted download. Run it again to update: your settings and sounds stay.
#
# It puts MicMix and its own Python (python-build-standalone, checked against its published checksum) in
# ~/Library/Application Support/MicMix and MicMix.app in ~/Applications, opens Existential Audio's page for
# BlackHole, the free virtual audio cable MicMix sends its sound through, and waits while you install it (MicMix
# doesn't bundle it), then opens MicMix. Uninstall: bash install.sh --uninstall
# --no-start (for tests): no launch, no questions, no waiting.
set -euo pipefail

DEST="$HOME/Library/Application Support/MicMix"
APP="$HOME/Applications/MicMix.app"
ID="com.silvades.micmix"
FILES=(micmix.pyw audio.py mac.py test_micmix.py README.md LICENSE.txt install.sh install.ps1 Install.cmd)
START=1 UNINSTALL=0
for arg in "$@"; do
    case "$arg" in
        --uninstall) UNINSTALL=1 ;;
        --no-start) START=0 ;;
        *) echo "Unknown option: $arg" >&2; exit 2 ;;
    esac
done

say() { printf '  %s\n' "$*"; }
step() { printf '\n\033[36m== %s\033[0m\n' "$*"; }
fail() { printf '\n  \033[31mSomething went wrong: %s\033[0m\n' "$*"; exit 1; }
ask() {  # a question to you even when this script came in through a pipe (curl | bash)
    local answer=""
    if [ "$START" = 1 ] && [ -r /dev/tty ]; then read -r -p "  $1 " answer < /dev/tty || true; fi
    printf '%s' "$answer"
}
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

stop_micmix() {  # asked to quit, it saves its settings; one that doesn't answer within 5 s is ended
    pgrep -xq MicMix || return 0
    osascript -e "tell application id \"$ID\" to quit" >/dev/null 2>&1 || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do pgrep -xq MicMix || return 0; sleep 0.5; done
    pkill -x MicMix || true
}

has_cable() {  # BlackHole (or VB-Cable for Mac) among the Mac's outputs
    "$1" -c "import sys; sys.path.insert(0, sys.argv[1]); import audio
sys.exit(0 if any(map(audio.is_cable, audio.devices('waveOut'))) else 1)" "$DEST" 2>/dev/null
}

get_python() {  # MicMix's own Python 3.12, with Tk: there unless missing or broken
    local py="$DEST/python/bin/python3"
    if "$py" -c 'import sys, tkinter; assert sys.version_info >= (3, 10)' 2>/dev/null; then return; fi
    local arch=x86_64
    if [ "$(sysctl -n hw.optional.arm64 2>/dev/null || echo 0)" = 1 ]; then arch=aarch64; fi  # Apple silicon, even under Rosetta
    say "Downloading Python 3.12 (python-build-standalone, about 20 MB)..."
    local json url name sums want have
    json=$(curl -fsSL https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest) ||
        fail "GitHub didn't answer. Check your internet connection, then run this again."
    url=$(printf '%s' "$json" | grep -o "https://[^\"]*/cpython-3\.12\.[0-9]*%2B[0-9]*-$arch-apple-darwin-install_only_stripped\.tar\.gz" | head -1)
    sums=$(printf '%s' "$json" | grep -o 'https://[^"]*/SHA256SUMS' | head -1)
    [ -n "$url" ] && [ -n "$sums" ] || fail "no Python 3.12 for this Mac in python-build-standalone's latest release."
    name=$(basename "$url" | sed 's/%2B/+/')
    curl -fL --progress-bar "$url" -o "$TMP/python.tar.gz" || fail "the Python download stopped. Run this again."
    want=$(curl -fsSL "$sums" | awk -v n="$name" '$2 == n {print $1}')
    have=$(shasum -a 256 "$TMP/python.tar.gz" | cut -d' ' -f1)
    [ -n "$want" ] && [ "$want" = "$have" ] || fail "that Python download doesn't match its published checksum, so it wasn't used."
    tar -xzf "$TMP/python.tar.gz" -C "$TMP"
    rm -rf "$DEST/python"
    mv "$TMP/python" "$DEST/python"
    "$py" -c 'import tkinter' || fail "the Python that came down can't open windows (no Tk)."
}

build_app() {  # MicMix.app: the launcher, its Info.plist and icon; ad-hoc signed. The same files make the same
    # signature on every install, so macOS keeps MicMix's mic and audio-recording permissions through updates
    rm -rf "$APP"
    mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
    cp "$DEST/assets/MicMix-launcher" "$APP/Contents/MacOS/MicMix"
    cp "$DEST/assets/Info.plist" "$APP/Contents/Info.plist"
    cp "$DEST/assets/micmix.icns" "$APP/Contents/Resources/MicMix.icns"
    chmod 755 "$APP/Contents/MacOS/MicMix"
    xattr -cr "$APP"  # no "downloaded from the internet" mark from a browser-extracted zip
    codesign --force --sign - "$APP" >/dev/null 2>&1 || fail "macOS wouldn't sign MicMix.app."
    /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$APP" \
        >/dev/null 2>&1 || true  # in Spotlight and Launchpad right away
}

if [ "$UNINSTALL" = 1 ]; then
    step "Removing MicMix"
    cd "$TMP"  # not inside the folder being removed
    stop_micmix
    rm -rf "$APP"
    keep=$(ask "Keep your sounds and settings, in case you install it again? (Y/n)")
    if [[ "$keep" =~ ^[[:space:]]*[nN] ]]; then
        rm -rf "$DEST"
        tccutil reset All "$ID" >/dev/null 2>&1 || true  # and its mic and audio-recording permissions
    elif [ -d "$DEST" ]; then
        find "$DEST" -mindepth 1 -maxdepth 1 ! -name sounds ! -name micmix.json -exec rm -rf {} +
    fi
    say "MicMix is removed. If you kept it in the Dock, drag it out of the Dock."
    say "BlackHole stays installed: to remove it too, follow Existential Audio's uninstall steps."
    say "In Discord, set Input Device back to your real mic."
    exit 0
fi

step "macOS"
version=$(sw_vers -productVersion)
say "macOS $version"
IFS=. read -r major minor _ <<< "$version.0"
if [ "$major" -lt 11 ]; then fail "MicMix needs macOS 11 or later."; fi
if [ "$major" -lt 14 ] || { [ "$major" = 14 ] && [ "${minor:-0}" -lt 2 ]; }; then
    say "Music from (an app's sound) needs macOS 14.2 or later. Your mic, the noise gate and the soundboard work here."
fi

step "MicMix"
mkdir -p "$DEST/sounds"
here=$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)
if [ -n "$here" ] && [ -f "$here/micmix.pyw" ] && [ -f "$here/mac.py" ] && [ "$here" != "$DEST" ]; then
    from="$here"  # run from a download or a clone
else
    say "Downloading the latest MicMix from GitHub..."
    curl -fsSL https://github.com/benjaminhoffman-bit/micmix/archive/refs/heads/main.zip -o "$TMP/micmix.zip" ||
        fail "GitHub didn't answer. Check your internet connection, then run this again."
    unzip -q "$TMP/micmix.zip" -d "$TMP"
    from="$TMP/micmix-main"
fi
stop_micmix
for file in "${FILES[@]}"; do cp "$from/$file" "$DEST/"; done
rm -rf "$DEST/assets"
cp -R "$from/assets" "$DEST/assets"  # the SILVADES fonts (with their licenses), icons and the app's launcher
say "Installed in $DEST"

step "Python"
get_python
py="$DEST/python/bin/python3"
"$py" -c 'import sys, sysconfig; print(sys.base_prefix + "/lib/" + sysconfig.get_config_var("LDLIBRARY")); print(sys.base_prefix)' \
    > "$DEST/python.txt"  # for MicMix.app: the library it runs, and its home
[ -f "$(head -1 "$DEST/python.txt")" ] || fail "this Python has no library for MicMix.app to run."
say "Using $("$py" -c 'import sys; print(sys.version.split()[0])') in $DEST/python"

step "MicMix.app"
build_app
say "MicMix is in your Applications folder (~/Applications), in Launchpad and in Spotlight."

step "BlackHole, the virtual mic"
if has_cable "$py"; then
    say "Already installed."
elif [ "$START" = 0 ]; then
    say "Not installed yet: get BlackHole 2ch from https://existential.audio/blackhole/"
else
    say "BlackHole is a free virtual audio cable from Existential Audio, and its own installer has to add it."
    say "Opening its page:"
    say "  1. Get BlackHole 2ch there and download its installer."
    say "  2. Open the downloaded .pkg and click through it (it asks for your Mac's password: it adds a driver)."
    say "  (With Homebrew, brew install blackhole-2ch does the same.)"
    say "This window waits and carries on by itself once BlackHole appears."
    open "https://existential.audio/blackhole/"
    deadline=$((SECONDS + 1800))
    until has_cable "$py" || [ "$SECONDS" -ge "$deadline" ]; do sleep 3; done
    if has_cable "$py"; then say "BlackHole is in."; else say "No BlackHole yet. Install it, then pick it under Send to in MicMix."; fi
fi

step "Last step, in Discord: User Settings > Voice & Video"
say "Input Device: BlackHole 2ch"
say "Input Profile: Custom. Noise Suppression: None. Echo Cancellation and Automatic Gain Control: off."
say "Input Sensitivity: turn off \"Automatically determine\" and drag the slider all the way left."
say "MicMix asks for your mic the first time, and to record other apps' sound once you pick Music from: allow both."
if [ "$START" = 1 ]; then open "$APP"; fi
