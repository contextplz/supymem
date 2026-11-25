#!/bin/sh
set -eu

case "$0" in
    /*) launcher=$0 ;;
    *) launcher=$(pwd)/$0 ;;
esac
case "$launcher" in
    *.AppImage.sh) executable=${launcher%.sh}; appimage=1 ;;
    *-launch) executable=${launcher%-launch}; appimage=0 ;;
    *) printf '%s\n' 'Download your app and its matching .AppImage.sh launcher into the same folder, then run the launcher.' >&2; exit 1 ;;
esac

if [ ! -x "$executable" ]; then
    printf 'Make your app executable with chmod +x "%s", then run your launcher again.\n' "$executable" >&2
    exit 1
fi

if [ "${1-}" != '--local-stack-smoke' ]; then
    if [ -z "${DISPLAY-}" ] && [ -z "${WAYLAND_DISPLAY-}" ]; then
        printf '%s\n' 'Your desktop app needs a graphical session. Sign in to your Linux desktop and reopen your app. On a headless server, run ./start --web from your checkout.' >&2
        exit 1
    fi
    if [ "$(id -u)" = 0 ]; then
        printf '%s\n' 'Open your app as your normal desktop user, without sudo. On a headless server, run ./start --web from your checkout.' >&2
        exit 1
    fi
    if [ -z "${WEBKIT_DISABLE_DMABUF_RENDERER+x}" ]; then
        # shortcut: accessible devices can still have driver bugs, add a driver-specific check when a reproducible failure needs automatic recovery.
        renderer=0
        for device in /dev/dri/renderD*; do
            if [ -r "$device" ] && [ -w "$device" ]; then renderer=1; break; fi
        done
        if [ "$renderer" = 0 ]; then export WEBKIT_DISABLE_DMABUF_RENDERER=1; fi
    fi
fi

if [ "$appimage" = 1 ] && [ "${1-}" != '--appimage-extract-and-run' ]; then
    ldconfig=$(command -v ldconfig || printf '%s' /sbin/ldconfig)
    if ! { [ -c /dev/fuse ] && [ -r /dev/fuse ] && [ -w /dev/fuse ] && fusermount -V >/dev/null 2>&1 && "$ldconfig" -p 2>/dev/null | grep -q 'libfuse\.so\.2'; }; then
        exec "$executable" --appimage-extract-and-run "$@"
    fi
fi
exec "$executable" "$@"
