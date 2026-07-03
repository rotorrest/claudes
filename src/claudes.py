#!/usr/bin/env python3
"""claudes / claude-monitor — monitor de sesiones de Claude Code en esta máquina.

Lee el estado que Claude Code publica en ~/.claude/sessions/<pid>.json
y muestra qué sesiones están trabajando, cuáles terminaron su turno y
cuáles están bloqueadas esperando que apruebes algo.

Además muestra métricas del sistema: CPU, load, RAM, presión de memoria,
swap, disco libre, consumo de Docker (por contenedor con la tecla d),
batería, temperatura de batería y throttling térmico. La temperatura del
CPU en Apple Silicon requiere sudo (powermetrics), así que se usa la de
la batería como proxy + el estado de throttling de pmset.

Uso:
  claudes                  # snapshot
  claudes -w [seg]         # modo watch, refresca cada N segundos (default 3)
  claudes focus <id>       # enfoca la pestaña de esa sesión (sessionId o pid)
  claudes update           # se auto-actualiza desde el último release de GitHub
  claudes --json           # snapshot de sesiones en JSON (para scripts/statuslines)
  claudes --version        # muestra la versión
  claude-monitor           # igual, pero arranca en modo watch directo

En modo watch revisa (máx. 1 vez al día) si hay versión nueva en GitHub y
lo avisa en el pie de pantalla. Exportar CLAUDES_NO_UPDATE_CHECK=1 lo apaga;
es la única llamada de red que hace la herramienta.

En modo watch: presiona la tecla de una fila (1-9, a…) para saltar a esa
sesión — pestaña exacta en Terminal/iTerm2 (por tty), ventana del proyecto
en Cursor/VS Code/Ghostty/Windsurf (por título) — cruzando Spaces/pantallas;
d para detalle de Docker; q para salir.
"""

import glob
import json
import os
import re
import select
import shutil
import subprocess
import sys
import termios
import threading
import time

__version__ = "0.1.0"
GITHUB_REPO = "rotorrest/claudes"

SESSIONS_DIR = os.path.expanduser("~/.claude/sessions")
PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
HOME = os.path.expanduser("~")
UPDATE_STAMP = os.path.expanduser("~/.cache/claudes/latest-version")

RED = "\x1b[31m"
YELLOW = "\x1b[33m"
GREEN = "\x1b[32m"
DIM = "\x1b[2m"
BOLD = "\x1b[1m"
RESET = "\x1b[0m"

GROUPS = [
    ("waiting", RED, "ESPERAN TU ACCIÓN"),
    ("idle", YELLOW, "PARADAS · esperan tu respuesta"),
    ("busy", GREEN, "TRABAJANDO"),
]

KEYS = "123456789abcefghijklmnoprstuvwxyz"  # sin q (salir) ni d (docker)

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
CTRL_RE = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")
NCPU = os.cpu_count() or 1


def clean(s):
    """Quita caracteres de control de strings externos (sessions/transcripts)
    para que no puedan inyectar secuencias de escape en la terminal."""
    return CTRL_RE.sub("", str(s))

# ── métricas del sistema ────────────────────────────────────────────────


def run_out(cmd, timeout=5):
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        ).stdout
    except Exception:
        return ""


try:
    MEM_TOTAL = int(run_out(["sysctl", "-n", "hw.memsize"]).strip())
except ValueError:
    MEM_TOTAL = 0


def vlen(s):
    return len(ANSI_RE.sub("", s))


def pad_between(left, right, width):
    return left + " " * max(1, width - vlen(left) - vlen(right)) + right


def fmt_size(n):
    if n >= 100 * 2**30:
        return f"{n / 2**30:.0f}G"
    if n >= 2**30:
        return f"{n / 2**30:.1f}G"
    if n >= 2**20:
        return f"{n / 2**20:.0f}M"
    return f"{n / 2**10:.0f}K"


def pct_color(pct):
    return GREEN if pct < 60 else YELLOW if pct < 85 else RED


def meter(pct, width=10):
    pct = max(0.0, min(100.0, pct))
    filled = int(round(pct / 100 * width))
    return (pct_color(pct) + "█" * filled + RESET
            + DIM + "░" * (width - filled) + RESET)


def cpu_pct():
    out = run_out(["ps", "-A", "-o", "%cpu="])
    try:
        total = sum(float(x) for x in out.split())
    except ValueError:
        return 0.0
    return min(100.0, total / NCPU)


def ram_used_bytes():
    out = run_out(["vm_stat"])
    m = re.search(r"page size of (\d+)", out)
    page = int(m.group(1)) if m else 16384

    def pages(label):
        m = re.search(re.escape(label) + r":\s+(\d+)", out)
        return int(m.group(1)) if m else 0

    # como Activity Monitor: app (activas) + wired + comprimidas
    return (pages("Pages active") + pages("Pages wired down")
            + pages("Pages occupied by compressor")) * page


def mem_pressure():
    v = run_out(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"]).strip()
    return {"1": ("normal", GREEN), "2": ("alta", YELLOW),
            "4": ("crítica", RED)}.get(v, ("?", DIM))


def swap_used_bytes():
    out = run_out(["sysctl", "-n", "vm.swapusage"])
    m = re.search(r"used = ([\d.]+)([KMG])", out)
    if not m:
        return 0
    return int(float(m.group(1)) * {"K": 2**10, "M": 2**20, "G": 2**30}[m.group(2)])


def parse_docker_size(s):
    m = re.match(r"([\d.]+)\s*([A-Za-z]+)", s.strip())
    if not m:
        return 0
    mult = {"b": 1, "kb": 1000, "kib": 2**10, "mb": 10**6, "mib": 2**20,
            "gb": 10**9, "gib": 2**30, "tb": 10**12, "tib": 2**40}
    return int(float(m.group(1)) * mult.get(m.group(2).lower(), 1))


def docker_stats(timeout=8):
    try:
        p = subprocess.run(
            ["docker", "stats", "--no-stream", "--format",
             "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}"],
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        return {"state": "missing"}
    except Exception:
        return {"state": "off"}
    if p.returncode != 0:
        return {"state": "off"}
    rows = []
    for line in p.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        name, cpup, mem = parts
        try:
            cpu = float(cpup.rstrip("%"))
        except ValueError:
            cpu = 0.0
        used, _, limit = mem.partition("/")
        rows.append({"name": name, "cpu": cpu,
                     "mem": parse_docker_size(used),
                     "limit": parse_docker_size(limit)})
    rows.sort(key=lambda r: -r["mem"])
    return {"state": "ok", "rows": rows,
            "cpu": sum(r["cpu"] for r in rows),
            "mem": sum(r["mem"] for r in rows),
            "limit": max((r["limit"] for r in rows), default=0)}


def battery_info():
    out = run_out(["pmset", "-g", "batt"])
    m = re.search(r"(\d+)%", out)
    pct = int(m.group(1)) if m else None
    io = run_out(["ioreg", "-rn", "AppleSmartBattery"])
    m = re.search(r'"Temperature"\s*=\s*(\d+)', io)
    temp = int(m.group(1)) / 100.0 if m else None
    return {"pct": pct, "ac": "AC Power" in out, "temp": temp}


def thermal_limit():
    """CPU_Speed_Limit de pmset -g therm; None = sin throttling registrado."""
    m = re.search(r"CPU_Speed_Limit\s*=\s*(\d+)", run_out(["pmset", "-g", "therm"]))
    return int(m.group(1)) if m else None


SLOW = {"docker": None, "batt": None, "therm": None, "update": None}


def refresh_slow():
    SLOW["batt"] = battery_info()
    SLOW["therm"] = thermal_limit()
    SLOW["docker"] = docker_stats()


def slow_loop(interval=10):
    try:
        check_update_once()
    except Exception:
        pass
    while True:
        try:
            refresh_slow()
        except Exception:
            pass
        time.sleep(interval)


def sys_block(width, docker_detail=False):
    """Líneas con métricas del sistema para el encabezado del monitor."""
    cpu = cpu_pct()
    load1 = os.getloadavg()[0]
    ram = ram_used_bytes()
    ram_pct = ram / MEM_TOTAL * 100 if MEM_TOTAL else 0.0
    press_txt, press_col = mem_pressure()
    swap = swap_used_bytes()
    disk = shutil.disk_usage("/")

    left = (f" {BOLD}CPU{RESET} {pct_color(cpu)}{cpu:3.0f}%{RESET} {meter(cpu)}"
            f" {DIM}load {load1:.1f}/{NCPU}{RESET}")
    right = (f"{BOLD}RAM{RESET} {pct_color(ram_pct)}{fmt_size(ram)}{RESET}"
             f"{DIM}/{fmt_size(MEM_TOTAL)}{RESET} {meter(ram_pct)}"
             f" presión {press_col}{press_txt}{RESET}")
    if swap:
        right += f" {DIM}· swap {fmt_size(swap)}{RESET}"
    lines = [pad_between(left, right + " ", width)]

    d = SLOW["docker"]
    if d is None:
        dtxt = f" {BOLD}DOCKER{RESET} {DIM}midiendo…{RESET}"
    elif d["state"] == "ok":
        pct = d["mem"] / d["limit"] * 100 if d["limit"] else 0
        dtxt = (f" {BOLD}DOCKER{RESET} {len(d['rows'])} cont"
                f" · cpu {d['cpu']:.1f}%"
                f" · ram {pct_color(pct)}{fmt_size(d['mem'])}{RESET}"
                f"{DIM}/{fmt_size(d['limit'])}{RESET}")
    elif d["state"] == "off":
        dtxt = f" {BOLD}DOCKER{RESET} {DIM}apagado{RESET}"
    else:
        dtxt = f" {BOLD}DOCKER{RESET} {DIM}no instalado{RESET}"

    parts = []
    b = SLOW["batt"]
    if b and b["pct"] is not None:
        btxt = f"{BOLD}BAT{RESET} {b['pct']}%"
        btxt += f" {DIM}AC{RESET}" if b["ac"] else ""
        if b["temp"] is not None:
            t = b["temp"]
            tcol = GREEN if t < 38 else YELLOW if t < 43 else RED
            btxt += f" · {tcol}{t:.1f}°C{RESET}"
        tl = SLOW["therm"]
        if tl is not None and tl < 100:
            btxt += f" · {RED}{BOLD}CPU limitada a {tl}%{RESET}"
        else:
            btxt += f" {DIM}· sin throttle{RESET}"
        parts.append(btxt)
    parts.append(f"{BOLD}SSD{RESET} {fmt_size(disk.free)} libres")
    lines.append(pad_between(dtxt, "   ".join(parts) + " ", width))

    if docker_detail and d and d.get("state") == "ok":
        for r in d["rows"]:
            lines.append(
                f"   {DIM}└{RESET} {r['name']:<32} {DIM}cpu{RESET} {r['cpu']:5.1f}%"
                f"  {DIM}ram{RESET} {fmt_size(r['mem'])}"
            )
    return lines

# El AXRaise vía System Events fuerza a macOS a saltar al Space/pantalla
# donde vive la ventana (activate solo no cruza Spaces de forma confiable).
# Requiere permiso de Accesibilidad; si no lo hay, el try lo ignora y queda
# el comportamiento clásico (enfocar app sin cambiar de Space).
TERMINAL_SCRIPT = '''
if application "Terminal" is running then
  tell application "Terminal"
    repeat with w in windows
      repeat with t in tabs of w
        if tty of t is "%s" then
          set selected tab of w to t
          set index of w to 1
          activate
          try
            tell application "System Events" to tell process "Terminal"
              perform action "AXRaise" of front window
            end tell
          end try
          return "ok"
        end if
      end repeat
    end repeat
  end tell
end if
return "no"
'''

ITERM_SCRIPT = '''
if application "iTerm2" is running then
  tell application "iTerm2"
    repeat with w in windows
      repeat with t in tabs of w
        repeat with s in sessions of t
          if tty of s is "%s" then
            select w
            select t
            select s
            activate
            try
              tell application "System Events" to tell process "iTerm2"
                perform action "AXRaise" of front window
              end tell
            end try
            return "ok"
          end if
        end repeat
      end repeat
    end repeat
  end tell
end if
return "no"
'''


# ── auto-update ─────────────────────────────────────────────────────────


def parse_ver(v):
    try:
        return tuple(int(x) for x in str(v).split("."))
    except ValueError:
        return ()


def is_newer(a, b):
    return parse_ver(a) > parse_ver(b)


def gh_get(path, timeout=8):
    import urllib.request
    req = urllib.request.Request(
        f"https://api.github.com/repos/{GITHUB_REPO}{path}",
        headers={"Accept": "application/vnd.github+json",
                 "User-Agent": f"claudes/{__version__}"},
    )
    # nosec B310: URL fija https a api.github.com
    with urllib.request.urlopen(req, timeout=timeout) as r:  # nosec B310
        return json.load(r)


def check_update_once():
    """Aviso pasivo de versión nueva (máx. una consulta al día, fail-silent).

    Es la única llamada de red de la herramienta; CLAUDES_NO_UPDATE_CHECK=1
    la desactiva por completo."""
    if os.environ.get("CLAUDES_NO_UPDATE_CHECK"):
        return
    latest = None
    try:
        if time.time() - os.path.getmtime(UPDATE_STAMP) < 86400:
            with open(UPDATE_STAMP) as fh:
                latest = fh.read().strip() or None
    except OSError:
        pass
    if latest is None:
        try:
            latest = gh_get("/releases/latest").get("tag_name", "").lstrip("v")
            os.makedirs(os.path.dirname(UPDATE_STAMP), exist_ok=True)
            with open(UPDATE_STAMP, "w") as fh:
                fh.write(latest)
        except Exception:
            return
    if latest and is_newer(latest, __version__):
        SLOW["update"] = latest


def self_update():
    """Descarga el último release, verifica SHA256 y se reemplaza en sitio."""
    import hashlib
    import tempfile
    import urllib.request

    print(f"claudes v{__version__} · buscando release en {GITHUB_REPO}…")
    try:
        rel = gh_get("/releases/latest", timeout=15)
    except Exception as e:
        sys.exit(f"error consultando GitHub: {e}")
    ver = rel.get("tag_name", "").lstrip("v")
    if not is_newer(ver, __version__):
        print(f"ya estás en la última versión (v{__version__})")
        return
    assets = {a.get("name"): a.get("browser_download_url")
              for a in rel.get("assets", [])}
    for need in ("claudes", "claude-notify", "SHA256SUMS"):
        if not assets.get(need):
            sys.exit(f"el release v{ver} no trae el asset '{need}'")

    def fetch(url):
        if not str(url).startswith("https://"):
            sys.exit(f"URL no-https en el release — aborto: {url}")
        req = urllib.request.Request(
            url, headers={"User-Agent": f"claudes/{__version__}"})
        with urllib.request.urlopen(req, timeout=30) as r:  # nosec B310
            return r.read()

    sums = {}
    for line in fetch(assets["SHA256SUMS"]).decode().splitlines():
        parts = line.split()
        if len(parts) == 2:
            sums[parts[1].lstrip("*")] = parts[0]

    bin_dir = os.path.dirname(os.path.realpath(__file__))
    for name in ("claudes", "claude-notify"):
        data = fetch(assets[name])
        digest = hashlib.sha256(data).hexdigest()
        if digest != sums.get(name):
            sys.exit(f"SHA256 de '{name}' no coincide con SHA256SUMS — aborto")
        dest = os.path.join(bin_dir, name)
        fd, tmp = tempfile.mkstemp(dir=bin_dir, prefix=f".{name}.")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.chmod(tmp, 0o755)
            os.replace(tmp, dest)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        print(f"  ✓ {dest}")
    try:
        os.unlink(UPDATE_STAMP)
    except OSError:
        pass
    print(f"actualizado a v{ver}")


# Detección de la app que contiene la sesión (subiendo por el árbol de
# procesos). Terminal/iTerm2 se enfocan por tty (pestaña exacta); los editores
# (Cursor/VS Code/Windsurf) vía `open -a <app> <cwd>`, que reutiliza y enfoca
# la ventana que ya tiene esa carpeta abierta — cruza Spaces nativamente,
# cosa que System Events no puede (solo ve ventanas del Space actual).
APP_MARKERS = (
    ("Cursor", "Cursor"),
    ("iTerm", "iTerm2"),
    ("Terminal.app", "Terminal"),
    ("Code", "Visual Studio Code"),
    ("Windsurf", "Windsurf"),
    ("Ghostty", "Ghostty"),
)
EDITOR_APPS = ("Cursor", "Visual Studio Code", "Windsurf")


def host_app(pid):
    for _ in range(10):
        if not pid or pid <= 1:
            return None
        out = run_out(["ps", "-o", "ppid=,command=", "-p", str(pid)])
        parts = out.strip().split(None, 1)
        if not parts:
            return None
        cmd = parts[1] if len(parts) > 1 else ""
        for marker, app in APP_MARKERS:
            if marker in cmd:
                return app
        try:
            pid = int(parts[0])
        except ValueError:
            return None
    return None


# Último recurso: por título de ventana (solo ve el Space actual).
TITLE_APPS = ("Cursor", "Code", "Ghostty", "Windsurf")

TITLE_SCRIPT = '''
tell application "System Events"
  if exists process "%(app)s" then
    tell process "%(app)s"
      repeat with w in windows
        if name of w contains "%(title)s" then
          set frontmost to true
          perform action "AXRaise" of w
          return "ok"
        end if
      end repeat
    end tell
  end if
end tell
return "no"
'''


def focus_title(cwd):
    proj = re.sub(r'["\\\\]', "", os.path.basename((cwd or "").rstrip("/")))
    if not proj:
        return False
    for app in TITLE_APPS:
        try:
            out = subprocess.run(
                ["osascript", "-e",
                 TITLE_SCRIPT % {"app": app, "title": proj}],
                capture_output=True, text=True, timeout=10,
            )
            if out.stdout.strip() == "ok":
                return True
        except Exception:
            pass
    return False


def focus_row(tty, cwd, pid=None):
    """Enfoca la sesión: tty exacto, ventana del editor, o título como último recurso."""
    app = host_app(pid) if pid else None
    if app in (None, "Terminal", "iTerm2") and tty and focus_tty(tty):
        return True
    if app in EDITOR_APPS and cwd and os.path.isdir(cwd):
        try:
            subprocess.run(["open", "-a", app, cwd],
                           capture_output=True, timeout=10)
            return True
        except Exception:
            pass
    return focus_title(cwd)


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def ttys_for(pids):
    if not pids:
        return {}
    out = subprocess.run(
        ["ps", "-o", "pid=,tty=", "-p", ",".join(map(str, pids))],
        capture_output=True, text=True,
    ).stdout
    ttys = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and re.fullmatch(r"ttys\d+", parts[1]):
            ttys[int(parts[0])] = parts[1].replace("ttys", "s")
    return ttys


def focus_tty(short_tty):
    """Enfoca la pestaña de Terminal/iTerm2 cuyo tty coincide (ej. 's016')."""
    if not re.fullmatch(r"s\d+", short_tty or ""):
        return False  # nunca interpolar algo raro en el AppleScript
    dev = "/dev/tty" + short_tty
    for script in (TERMINAL_SCRIPT, ITERM_SCRIPT):
        try:
            out = subprocess.run(
                ["osascript", "-e", script % dev],
                capture_output=True, text=True, timeout=10,
            )
            if out.stdout.strip() == "ok":
                return True
        except Exception:
            pass
    return False


def focus_session(ident):
    """Enfoca la pestaña de la sesión cuyo sessionId o pid coincide con ident."""
    for f in glob.glob(os.path.join(SESSIONS_DIR, "*.json")):
        try:
            with open(f) as fh:
                d = json.load(fh)
        except (ValueError, OSError):
            continue
        if d.get("sessionId") != ident and str(d.get("pid")) != ident:
            continue
        pid = d.get("pid")
        if not pid or not alive(pid):
            return False
        tty = ttys_for([pid]).get(pid)
        return focus_row(tty, d.get("cwd", ""), pid)
    return False


def fmt_age(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def shorten_path(cwd, max_len):
    p = cwd.replace(HOME, "~")
    if len(p) <= max_len:
        return p
    parts = p.split("/")
    while len(parts) > 2 and len("…/" + "/".join(parts[1:])) > max_len:
        parts.pop(1)
    short = parts[0] + "/…/" + "/".join(parts[2:]) if len(parts) > 2 else p
    return short[:max_len]


def session_jsonl(cwd, session_id):
    proj = os.path.join(PROJECTS_DIR, re.sub(r"[^A-Za-z0-9]", "-", cwd))
    return os.path.join(proj, f"{session_id}.jsonl")


def last_assistant_text(cwd, session_id, max_len):
    """Último texto que escribió Claude en esa sesión (para saber en qué quedó)."""
    if not session_id:
        return ""
    path = session_jsonl(cwd, session_id)
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            fh.seek(max(0, size - 131072))
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") != "assistant" or d.get("isSidechain"):
            continue
        content = (d.get("message") or {}).get("content") or []
        texts = [b.get("text", "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        if not texts:
            continue
        snippet = clean(re.sub(r"[*#`]+", "", re.sub(r"\s+", " ", texts[-1]))).strip()
        if len(snippet) > max_len:
            snippet = snippet[: max_len - 1] + "…"
        return snippet
    return ""


def collect():
    now = time.time()
    rows = []
    for f in glob.glob(os.path.join(SESSIONS_DIR, "*.json")):
        try:
            with open(f) as fh:
                d = json.load(fh)
        except (ValueError, OSError):
            continue
        pid = d.get("pid")
        if not pid or not alive(pid):
            continue
        since = d.get("statusUpdatedAt", d.get("updatedAt", now * 1000)) / 1000
        # sessionId se usa para armar rutas: solo formato UUID-ish
        sid = str(d.get("sessionId", ""))
        if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", sid):
            sid = ""
        cwd = d.get("cwd", "?")
        status = d.get("status", "?")
        if status not in ("waiting", "idle", "busy"):
            status = "busy"  # p.ej. "shell": está corriendo un comando
        # idle pero con el transcript escribiéndose hace poco = subagentes
        # en background siguen trabajando aunque el turno principal terminó
        bg = False
        if status == "idle" and sid:
            try:
                if now - os.path.getmtime(session_jsonl(cwd, sid)) < 90:
                    bg, status = True, "busy"
            except OSError:
                pass
        rows.append({
            "pid": pid,
            "name": clean(d.get("name", "?")),
            "cwd": clean(cwd),
            "sessionId": sid,
            "status": status,
            "bg": bg,
            "age": now - since,
            "waitingFor": clean(d.get("waitingFor", "")),
        })
    rows.sort(key=lambda r: -r["age"])
    return rows


def render(rows, width, show_keys=False, docker_detail=False):
    ttys = ttys_for([r["pid"] for r in rows])
    name_w = min(max([len(r["name"]) for r in rows] or [10]), 30)
    key_w = 3 if show_keys else 0
    indent = 12 + key_w
    lines = []
    keymap = {}
    key_i = 0

    counts = {k: sum(1 for r in rows if r["status"] == k) for k, _, _ in GROUPS}
    head_l = f" {BOLD}CLAUDE MONITOR{RESET}{DIM} · {len(rows)} sesiones{RESET}"
    head_r = time.strftime("%H:%M:%S")
    pad = width - (17 + len(f" · {len(rows)} sesiones")) - len(head_r) - 1
    lines.append(head_l + " " * max(1, pad) + DIM + head_r + RESET)
    lines.append(DIM + "─" * width + RESET)
    lines.extend(sys_block(width, docker_detail))
    lines.append(DIM + "─" * width + RESET)

    if not rows:
        lines.append(f"{DIM} No hay sesiones de Claude Code corriendo.{RESET}")
        return "\n".join(lines), keymap

    for status, color, title in GROUPS:
        group = [r for r in rows if r["status"] == status]
        if not group:
            continue
        lines.append("")
        lines.append(f"{' ' * (1 + key_w)}{color}{BOLD}▍{title}{RESET} {DIM}({len(group)}){RESET}")
        for r in group:
            tty = ttys.get(r["pid"])
            key = ""
            if show_keys and key_i < len(KEYS):
                key = KEYS[key_i]
                key_i += 1
                r["_tty"] = tty
                keymap[key] = r
            key_part = f" {DIM}{key or ' '}{RESET} " if show_keys else " "
            right = f"{tty or '?'} · {r['pid']} "
            age = fmt_age(r["age"])
            path_max = max(12, width - (10 + key_w + name_w + 2) - len(right) - 2)
            path = shorten_path(r["cwd"], path_max)
            left_visible = (1 + key_w) + 2 + 6 + 2 + name_w + 2 + len(path)
            gap = max(1, width - left_visible - len(right))
            lines.append(
                f"{key_part}{color}●{RESET} {age:>6}  {BOLD}{r['name']:<{name_w}}{RESET}"
                f"  {path}{' ' * gap}{DIM}{right}{RESET}"
            )
            if r["status"] == "waiting" and r["waitingFor"]:
                lines.append(f"{' ' * indent}{RED}⏸ esperando: {r['waitingFor']}{RESET}")
            if r.get("bg"):
                lines.append(f"{' ' * indent}{GREEN}⚙{RESET} {DIM}agentes en "
                             f"background activos{RESET}")
            if r["status"] in ("waiting", "idle"):
                snippet = last_assistant_text(
                    r["cwd"], r["sessionId"], max_len=width - indent - 3
                )
                if snippet:
                    lines.append(f"{' ' * indent}{DIM}└ {snippet}{RESET}")

    lines.append("")
    summary = []
    if counts["waiting"]:
        summary.append(f"{RED}{BOLD}{counts['waiting']} esperan tu acción{RESET}")
    if counts["idle"]:
        summary.append(f"{YELLOW}{counts['idle']} paradas{RESET}")
    if counts["busy"]:
        summary.append(f"{GREEN}{counts['busy']} trabajando{RESET}")
    lines.append(" " + f"{DIM} · {RESET}".join(summary))
    return "\n".join(lines), keymap


def term_width():
    return min(shutil.get_terminal_size((110, 30)).columns, 160)


def watch_loop(interval):
    is_tty = sys.stdin.isatty()
    fd = sys.stdin.fileno() if is_tty else None
    old_attrs = None
    docker_detail = False
    threading.Thread(target=slow_loop, daemon=True).start()
    sys.stdout.write("\x1b[?1049h\x1b[?25l")  # alt-screen + cursor oculto
    try:
        if is_tty:
            old_attrs = termios.tcgetattr(fd)
            raw = termios.tcgetattr(fd)
            raw[3] &= ~(termios.ICANON | termios.ECHO)
            termios.tcsetattr(fd, termios.TCSANOW, raw)
        while True:
            out, keymap = render(collect(), term_width(), show_keys=is_tty,
                                 docker_detail=docker_detail)
            sys.stdout.write("\x1b[2J\x1b[H")
            print(out)
            hint = ("tecla = ir a esa pestaña · d docker · q salir" if is_tty
                    else "Ctrl+C para salir")
            print(f"\n{DIM} refresca cada {interval:g}s · {hint}{RESET}")
            if SLOW["update"]:
                print(f" {YELLOW}⬆ v{SLOW['update']} disponible — "
                      f"corre `claudes update`{RESET}")
            sys.stdout.flush()
            if is_tty:
                r, _, _ = select.select([sys.stdin], [], [], interval)
                if r:
                    ch = sys.stdin.read(1)
                    if ch in ("q", "Q"):
                        break
                    if ch in ("d", "D"):
                        docker_detail = not docker_detail
                        continue
                    row = keymap.get(ch)
                    if row:
                        focus_row(row["_tty"], row["cwd"], row["pid"])
            else:
                time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        if old_attrs is not None:
            termios.tcsetattr(fd, termios.TCSANOW, old_attrs)
        sys.stdout.write("\x1b[?1049l\x1b[?25h")
        sys.stdout.flush()


def main():
    args = sys.argv[1:]
    watch = None
    if os.path.basename(sys.argv[0]) == "claude-monitor" and not args:
        watch = 3.0
    if args and args[0] == "focus" and len(args) > 1:
        sys.exit(0 if focus_session(args[1]) else 1)
    if args and args[0] in ("update", "--update"):
        self_update()
        return
    if args and args[0] in ("-v", "--version"):
        print(f"claudes v{__version__}")
        return
    if args and args[0] == "--json":
        print(json.dumps(collect(), indent=2, ensure_ascii=False))
        return
    if args and args[0] in ("-w", "--watch"):
        watch = 3.0
        if len(args) > 1:
            try:
                watch = max(1.0, float(args[1]))
            except ValueError:
                pass
    elif args and args[0] in ("-h", "--help"):
        print(__doc__.strip())
        return

    if watch is None:
        refresh_slow()
        print(render(collect(), term_width(), docker_detail=True)[0])
    else:
        watch_loop(watch)


if __name__ == "__main__":
    main()
