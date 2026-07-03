#!/usr/bin/env python3
"""Hook de Claude Code → notificación nativa de macOS.

Recibe el JSON del hook por stdin (eventos Notification y Stop) y muestra
una notificación con el proyecto y el motivo. Si terminal-notifier está
instalado (brew install terminal-notifier), hacer clic en la notificación
enfoca la pestaña de Terminal/iTerm2 de esa sesión vía `claudes focus`.
Sin terminal-notifier cae al osascript de siempre (clic sin acción).
Usado desde ~/.claude/settings.json.
"""

import json
import os
import re
import shutil
import subprocess
import sys

CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


def find_claudes():
    # mismo directorio que este script (instalación normal), PATH, o ~/.local/bin
    here = os.path.join(os.path.dirname(os.path.realpath(__file__)), "claudes")
    if os.path.exists(here):
        return here
    return (shutil.which("claudes")
            or os.path.expanduser("~/.local/bin/claudes"))


CLAUDES = find_claudes()


def esc(s):
    s = CTRL_RE.sub(" ", s)  # newlines/controles romperían el AppleScript
    return s.replace("\\", "\\\\").replace('"', '\\"')


def find_terminal_notifier():
    # los hooks pueden correr con PATH mínimo; probar rutas típicas de brew
    tn = shutil.which("terminal-notifier")
    if tn:
        return tn
    for p in ("/opt/homebrew/bin/terminal-notifier",
              "/usr/local/bin/terminal-notifier"):
        if os.path.exists(p):
            return p
    return None


def main():
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return

    event = data.get("hook_event_name", "")
    cwd = data.get("cwd", "") or ""
    session_id = data.get("session_id", "") or ""
    # session_id termina dentro del -execute de terminal-notifier, que corre
    # con sh -c al hacer clic: solo dejar pasar formato UUID-ish
    if not re.fullmatch(r"[A-Za-z0-9-]{8,64}", session_id):
        session_id = ""
    proj = os.path.basename(cwd.rstrip("/")) or "claude"
    title = f"Claude · {proj}"

    if event == "Stop":
        # No notificar si el stop viene de un hook encadenado
        if data.get("stop_hook_active"):
            return
        # sin sonido: el hook afplay existente ya suena al terminar el turno
        body = "Terminó su turno — esperando tu respuesta"
        sound = None
    else:  # Notification: permisos, esperando input, etc.
        body = data.get("message") or "Claude necesita tu atención"
        sound = "Glass"

    tn = find_terminal_notifier()
    if tn:
        cmd = [tn, "-title", title, "-message", body,
               "-group", f"claude-{session_id or proj}"]
        if sound:
            cmd += ["-sound", sound]
        if session_id and os.path.exists(CLAUDES):
            cmd += ["-execute", f'"{CLAUDES}" focus {session_id}']
        subprocess.run(cmd, capture_output=True, timeout=10)
        return

    script = (
        f'display notification "{esc(body)}" '
        f'with title "{esc(title)}"'
        + (f' sound name "{sound}"' if sound else "")
    )
    subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)


if __name__ == "__main__":
    main()
