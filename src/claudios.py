#!/usr/bin/env python3
"""claudios / claude-monitor — monitor de sesiones de Claude Code en esta máquina.

Lee el estado que Claude Code publica en ~/.claude/sessions/<pid>.json
y muestra qué sesiones están trabajando, cuáles terminaron su turno y
cuáles están bloqueadas esperando que apruebes algo.

Además muestra métricas del sistema: CPU, load, RAM, presión de memoria,
swap, disco libre, consumo de Docker (por contenedor con la tecla d),
batería, temperatura de batería y throttling térmico. La temperatura del
CPU en Apple Silicon requiere sudo (powermetrics), así que se usa la de
la batería como proxy + el estado de throttling de pmset.

En el pie muestra el uso de tokens de las últimas 5 horas (leído de los
message.usage de los JSONL locales de ~/.claude/projects — cero red):
totales in/out/cache, costo API equivalente estimado, burn rate con
sparkline de los últimos 10 minutos, y top de sesiones con la tecla u.

Uso:
  claudios                  # snapshot
  claudios -w [seg]         # modo watch, refresca cada N segundos (default 3)
  claudios focus <id>       # enfoca la pestaña de esa sesión (sessionId o pid)
  claudios update           # se auto-actualiza desde el último release de GitHub
  claudios --json           # snapshot de sesiones en JSON (para scripts/statuslines)
  claudios --version        # muestra la versión
  claude-monitor           # igual, pero arranca en modo watch directo

En modo watch revisa (máx. 1 vez al día) si hay versión nueva en GitHub y
lo avisa en el pie de pantalla. Exportar CLAUDIOS_NO_UPDATE_CHECK=1 lo apaga;
es la única llamada de red que hace la herramienta.

En modo watch: muévete entre sesiones con ↑/↓ (o j/k) y Enter para ir a
la seleccionada, o presiona directo la tecla de una fila (1-9, a…) —
pestaña exacta en Terminal/iTerm2 (por tty), ventana del proyecto en
Cursor/VS Code/Ghostty/Windsurf — cruzando Spaces/pantallas.
Con → (o l) abres la conversación de la sesión seleccionada en vivo sin
salir de la terminal: ↑/↓ hace scroll, Enter salta a su terminal real,
← / Esc vuelve a la lista.
d para detalle de Docker; u para detalle de uso; q para salir.
"""

import datetime
import glob
import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import termios
import textwrap
import threading
import time

__version__ = "0.5.2"
GITHUB_REPO = "rotorrest/claude-monitor"

SESSIONS_DIR = os.path.expanduser("~/.claude/sessions")
PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
HOME = os.path.expanduser("~")
UPDATE_STAMP = os.path.expanduser("~/.cache/claudios/latest-version")

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

# sin q (salir), d (docker), u (uso), j/k (navegar) ni h/l (preview)
KEYS = "123456789abcefgimnoprstvwxyz"

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


SLOW = {"docker": None, "batt": None, "therm": None, "update": None,
        "usage": None}


def refresh_slow():
    SLOW["batt"] = battery_info()
    SLOW["therm"] = thermal_limit()
    SLOW["docker"] = docker_stats()


# ── uso de tokens/costo (últimas 5h, leído de los JSONL locales) ─────────
# Los transcripts de ~/.claude/projects traen message.usage por turno de
# assistant. Precios USD por MTok (input, output); cache write = 1.25x
# input, cache read = 0.1x input. Match por substring del model id.
USAGE_WINDOW = 5 * 3600
PRICES = (
    ("fable", (10.0, 50.0)),
    ("mythos", (10.0, 50.0)),
    ("opus-4-1", (15.0, 75.0)),
    ("opus-4-2025", (15.0, 75.0)),
    ("opus", (5.0, 25.0)),
    ("sonnet", (3.0, 15.0)),
    ("haiku-3", (0.8, 4.0)),
    ("haiku", (1.0, 5.0)),
)
USAGE = {"offsets": {}, "events": []}
SPARK_CH = "▁▂▃▄▅▆▇█"


def price_for(model):
    for key, prices in PRICES:
        if key in (model or ""):
            return prices
    return (5.0, 25.0)  # desconocido: asumir opus-tier


def fmt_tok(n):
    if n >= 10**9:
        return f"{n / 10**9:.1f}G"
    if n >= 10**6:
        return f"{n / 10**6:.1f}M"
    if n >= 10**3:
        return f"{n / 10**3:.0f}K"
    return f"{n:.0f}"


def parse_ts(s):
    try:
        return datetime.datetime.fromisoformat(
            str(s).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def scan_usage(tail=2 * 2**20):
    """Acumula eventos de usage de los JSONL con actividad reciente.
    Lectura incremental por offsets; la primera vez solo el tail del archivo."""
    now = time.time()
    cutoff = now - USAGE_WINDOW
    for path in glob.glob(os.path.join(PROJECTS_DIR, "*", "*.jsonl")):
        try:
            st = os.stat(path)
        except OSError:
            continue
        if st.st_mtime < cutoff:
            USAGE["offsets"].pop(path, None)
            continue
        off = USAGE["offsets"].get(path)
        if off is None:
            start = max(0, st.st_size - tail)
        elif st.st_size > off:
            start = off
        else:
            continue
        try:
            with open(path, "rb") as fh:
                fh.seek(start)
                data = fh.read()
        except OSError:
            continue
        USAGE["offsets"][path] = start + len(data)
        sid = os.path.basename(path).rsplit(".", 1)[0]
        proj = os.path.basename(os.path.dirname(path))
        for line in data.decode("utf-8", "replace").splitlines():
            if '"usage"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            usage = (d.get("message") or {}).get("usage")
            if not isinstance(usage, dict):
                continue
            ts = parse_ts(d.get("timestamp")) or now
            if ts < cutoff:
                continue
            USAGE["events"].append({
                "ts": ts,
                "in": usage.get("input_tokens") or 0,
                "out": usage.get("output_tokens") or 0,
                "cw": usage.get("cache_creation_input_tokens") or 0,
                "cr": usage.get("cache_read_input_tokens") or 0,
                "model": (d.get("message") or {}).get("model", ""),
                "sid": sid,
                "proj": proj,
            })
    USAGE["events"] = [e for e in USAGE["events"] if e["ts"] >= now - USAGE_WINDOW]


def session_names():
    names = {}
    for f in glob.glob(os.path.join(SESSIONS_DIR, "*.json")):
        try:
            with open(f) as fh:
                d = json.load(fh)
            names[d.get("sessionId", "")] = clean(d.get("name", ""))
        except (ValueError, OSError):
            pass
    return names


def usage_summary():
    """Resumen de la ventana: totales, costo API equivalente, burn y top."""
    now = time.time()
    events = USAGE["events"]
    if not events:
        return None
    tot = {"in": 0, "out": 0, "cw": 0, "cr": 0}
    cost = 0.0
    per_sess = {}
    buckets = [0] * 10  # últimos 10 min, 1 min por bucket (sparkline)
    for e in events:
        pin, pout = price_for(e["model"])
        c = (e["in"] * pin + e["out"] * pout
             + e["cw"] * pin * 1.25 + e["cr"] * pin * 0.1) / 1e6
        cost += c
        for k in tot:
            tot[k] += e[k]
        s = per_sess.setdefault(
            e["sid"], {"tok": 0, "cost": 0.0, "proj": e["proj"]})
        s["tok"] += e["in"] + e["out"] + e["cw"] + e["cr"]
        s["cost"] += c
        age_min = (now - e["ts"]) / 60
        if age_min < 10:
            buckets[9 - int(age_min)] += e["in"] + e["out"] + e["cw"]
    burn = sum(buckets) / 10  # tokens facturables/min (cache reads fuera)
    peak = max(buckets) or 1
    spark = "".join(SPARK_CH[min(7, int(b / peak * 7))] if b else SPARK_CH[0]
                    for b in buckets)
    names = session_names()
    grand = sum(s["tok"] for s in per_sess.values()) or 1
    top = sorted(per_sess.items(), key=lambda kv: -kv[1]["tok"])[:5]
    top_rows = [{"name": names.get(sid) or s["proj"][-24:] or sid[:8],
                 "tok": s["tok"], "cost": s["cost"],
                 "pct": s["tok"] / grand * 100} for sid, s in top]
    return {"in": tot["in"], "out": tot["out"], "cw": tot["cw"],
            "cr": tot["cr"], "total": sum(tot.values()), "cost": cost,
            "burn": burn, "spark": spark, "top": top_rows,
            "sessions": len(per_sess)}


def usage_block(width, detail=False):
    """Líneas del pie con el uso de la ventana de 5h."""
    u = SLOW.get("usage")
    if not u:
        return []
    left = (f" {BOLD}USO 5h{RESET} {fmt_tok(u['total'])} tokens"
            f" {DIM}· in {fmt_tok(u['in'])} · out {fmt_tok(u['out'])}"
            f" · cache w {fmt_tok(u['cw'])} r {fmt_tok(u['cr'])}{RESET}")
    right = f"~${u['cost']:.2f} {DIM}equiv. API{RESET} "
    top = u["top"][0] if u["top"] else None
    left2 = (f" {BOLD}BURN{RESET} {fmt_tok(u['burn'])} tok/min"
             f" {DIM}{u['spark']}{RESET}")
    right2 = (f"{DIM}top:{RESET} {top['name'][:24]} {DIM}{top['pct']:.0f}%{RESET} "
              if top else " ")
    lines = ["", pad_between(left, right, width),
             pad_between(left2, right2, width)]
    if detail:
        for t in u["top"]:
            lines.append(
                f"   {DIM}└{RESET} {t['name'][:32]:<32} {fmt_tok(t['tok']):>7}"
                f" {DIM}tok{RESET}  ~${t['cost']:.2f}  {DIM}{t['pct']:.0f}%{RESET}")
    return lines


def slow_loop(interval=10):
    try:
        check_update_once()
    except Exception:
        pass
    while True:
        try:
            refresh_slow()
            scan_usage()
            SLOW["usage"] = usage_summary()
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
                 "User-Agent": f"claudios/{__version__}"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:  # nosec B310
        return json.load(r)


def check_update_once():
    """Aviso pasivo de versión nueva (máx. una consulta al día, fail-silent).

    Es la única llamada de red de la herramienta; CLAUDIOS_NO_UPDATE_CHECK=1
    la desactiva por completo."""
    if os.environ.get("CLAUDIOS_NO_UPDATE_CHECK"):
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

    print(f"claudios v{__version__} · buscando release en {GITHUB_REPO}…")
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
    for need in ("claudios", "claude-notify", "SHA256SUMS"):
        if not assets.get(need):
            sys.exit(f"el release v{ver} no trae el asset '{need}'")

    def fetch(url):
        if not str(url).startswith("https://"):
            sys.exit(f"URL no-https en el release — aborto: {url}")
        req = urllib.request.Request(
            url, headers={"User-Agent": f"claudios/{__version__}"})
        with urllib.request.urlopen(req, timeout=30) as r:  # nosec B310
            return r.read()

    sums = {}
    for line in fetch(assets["SHA256SUMS"]).decode().splitlines():
        parts = line.split()
        if len(parts) == 2:
            sums[parts[1].lstrip("*")] = parts[0]

    bin_dir = os.path.dirname(os.path.realpath(__file__))
    for name in ("claudios", "claude-notify"):
        data = fetch(assets[name])
        digest = hashlib.sha256(data).hexdigest()
        if digest != sums.get(name):
            sys.exit(f"SHA256 de '{name}' no coincide con SHA256SUMS — aborto")
        dest = os.path.join(bin_dir, name)
        fd, tmp = tempfile.mkstemp(dir=bin_dir, prefix=f".{name}.")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            # nosec B103: es un ejecutable de CLI, 755 es el permiso correcto
            os.chmod(tmp, 0o755)  # nosec B103
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


# ── preview de conversación (dentro de la misma terminal) ───────────────


def compact_input(tool_input):
    """Resumen de una línea del input de un tool_use."""
    if not isinstance(tool_input, dict):
        return ""
    for key in ("command", "file_path", "pattern", "url", "prompt",
                "description", "query"):
        v = tool_input.get(key)
        if isinstance(v, str) and v.strip():
            return clean(re.sub(r"\s+", " ", v)).strip()
    try:
        return clean(json.dumps(tool_input, ensure_ascii=False))
    except (TypeError, ValueError):
        return ""


def load_transcript(cwd, session_id, max_bytes=262144):
    """Eventos legibles (rol, texto) del tail del JSONL de la sesión."""
    if not session_id:
        return []
    path = session_jsonl(cwd, session_id)
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            fh.seek(max(0, size - max_bytes))
            raw = fh.read().decode("utf-8", "replace")
    except OSError:
        return []
    lines = raw.splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]  # la primera puede venir cortada
    events = []
    for line in lines:
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("isSidechain"):
            continue
        msg = d.get("message") or {}
        content = msg.get("content")
        if d.get("type") == "user":
            if isinstance(content, str):
                events.append(("user", content))
            elif isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text":
                        events.append(("user", b.get("text", "")))
                    elif b.get("type") == "tool_result":
                        rc = b.get("content")
                        n = (len(rc) if isinstance(rc, str)
                             else sum(len(x.get("text", "")) for x in rc
                                      if isinstance(x, dict)) if isinstance(rc, list)
                             else 0)
                        events.append(("result", f"resultado ({fmt_tok(n)} chars)"))
        elif d.get("type") == "assistant":
            for b in content or []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and b.get("text", "").strip():
                    events.append(("claude", b.get("text", "")))
                elif b.get("type") == "tool_use":
                    detail = compact_input(b.get("input"))
                    events.append(("tool", f"{b.get('name', '?')}  {detail}"))
    return events


PREVIEW_STYLE = {
    "user":   (YELLOW + BOLD, "❯ "),
    "claude": ("", "  "),
    "tool":   (DIM, "  ⚙ "),
    "result": (DIM, "    ↳ "),
}


def render_preview(row, width, height, offset):
    """Frame full-screen con la conversación de una sesión, scrolleable."""
    status_col = {"waiting": RED, "idle": YELLOW, "busy": GREEN}.get(
        row["status"], DIM)
    head_l = (f" {BOLD}{row['name'][:40]}{RESET}  {status_col}●{RESET}"
              f" {DIM}{shorten_path(row['cwd'], 40)}{RESET}")
    head_r = f"{DIM}{row['pid']} · {time.strftime('%H:%M:%S')}{RESET} "
    head = [pad_between(head_l, head_r, width), DIM + "─" * width + RESET]

    body = []
    for role, text in load_transcript(row["cwd"], row["sessionId"]):
        color, prefix = PREVIEW_STYLE[role]
        text = clean(re.sub(r"\s*\n\s*", " " if role != "claude" else "\n", text))
        if role in ("tool", "result"):
            text = text[:width - len(prefix) - 1]
        pad = " " * len(prefix)
        first = True
        for para in text.split("\n"):
            wrapped = textwrap.wrap(para, width=width - len(prefix) - 1) or [""]
            for w in wrapped:
                lead = prefix if first else pad
                body.append(f"{color}{lead}{w}{RESET}" if color
                            else f"{lead}{w}")
                first = False
        if role in ("user", "claude"):
            body.append("")

    view_h = max(4, height - len(head) - 3)
    offset = max(0, min(offset, max(0, len(body) - view_h)))
    end = len(body) - offset
    window = body[max(0, end - view_h):end]
    if offset:
        head[1] = pad_between(DIM + "─" * (width - 14) + RESET,
                              f"{YELLOW}↓ {offset} más{RESET} ", width)
    return "\n".join(head + window), offset


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


def render(rows, width, height=0, show_keys=False, docker_detail=False,
           usage_detail=False, selected_pid=None):
    """Arma el frame. Si height > 0, garantiza que quepa: primero quita los
    snippets, y si aún no entra trunca filas con «… +N sesiones más»."""
    ttys = ttys_for([r["pid"] for r in rows])
    name_w = min(max([len(r["name"]) for r in rows] or [10]), 30)
    key_w = 3 if show_keys else 0
    indent = 12 + key_w

    counts = {k: sum(1 for r in rows if r["status"] == k) for k, _, _ in GROUPS}
    head = []
    head_l = f" {BOLD}CLAUDE MONITOR{RESET}{DIM} · {len(rows)} sesiones{RESET}"
    head_r = time.strftime("%H:%M:%S")
    pad = width - (17 + len(f" · {len(rows)} sesiones")) - len(head_r) - 1
    head.append(head_l + " " * max(1, pad) + DIM + head_r + RESET)
    head.append(DIM + "─" * width + RESET)
    head.extend(sys_block(width, docker_detail))
    head.append(DIM + "─" * width + RESET)
    if height and height < len(head) + 9:
        head = head[:2]  # terminal muy bajita: fuera las métricas de sistema

    summary = []
    if counts["waiting"]:
        summary.append(f"{RED}{BOLD}{counts['waiting']} esperan tu acción{RESET}")
    if counts["idle"]:
        summary.append(f"{YELLOW}{counts['idle']} paradas{RESET}")
    if counts["busy"]:
        summary.append(f"{GREEN}{counts['busy']} trabajando{RESET}")
    summary_line = (" " + f"{DIM} · {RESET}".join(summary) if summary
                    else f"{DIM} sin sesiones{RESET}")

    # pie anclado al fondo: separador + resumen + uso — el espacio del
    # medio queda libre para más claudios
    footer = [DIM + "─" * width + RESET, summary_line]
    if not height or height >= 24:
        ub = usage_block(width, usage_detail)
        if ub:
            footer += ub[1:]  # sin la línea en blanco inicial
    reserve = 3 if SLOW.get("update") else 2  # hint (+aviso de versión)

    def assemble(lines):
        if height:
            pad = max(1, height - reserve - len(lines) - len(footer))
            return lines + [""] * pad + footer
        return lines + [""] + footer

    if not rows:
        head.append(f"{DIM} No hay sesiones de Claude Code corriendo.{RESET}")
        return "\n".join(assemble(head)), {}

    def build(with_snippets, budget):
        lines = list(head)
        keymap = {}
        key_i = 0
        hidden = 0
        for status, color, title in GROUPS:
            group = [r for r in rows if r["status"] == status]
            if not group:
                continue
            if budget and len(lines) + 2 >= budget:
                hidden += len(group)
                continue
            lines.append("")
            lines.append(f"{' ' * (1 + key_w)}{color}{BOLD}▍{title}{RESET} {DIM}({len(group)}){RESET}")
            for r in group:
                if budget and len(lines) >= budget:
                    hidden += 1
                    continue
                tty = ttys.get(r["pid"])
                key = ""
                if show_keys and key_i < len(KEYS):
                    key = KEYS[key_i]
                    key_i += 1
                    r["_tty"] = tty
                    keymap[key] = r
                sel = selected_pid is not None and r["pid"] == selected_pid
                pointer = f"{BOLD}▸{RESET}" if sel else " "
                key_part = (f"{pointer}{DIM}{key or ' '}{RESET} "
                            if show_keys else " ")
                # subrayado en la fila seleccionada (mismo ancho visible)
                name_fmt = (f"\x1b[4m{r['name'][:name_w]:<{name_w}}\x1b[24m"
                            if sel else f"{r['name'][:name_w]:<{name_w}}")
                right = f"{tty or '?'} · {r['pid']} "
                age = fmt_age(r["age"])
                path_max = max(12, width - (10 + key_w + name_w + 2) - len(right) - 2)
                path = shorten_path(r["cwd"], path_max)
                left_visible = (1 + key_w) + 2 + 6 + 2 + name_w + 2 + len(path)
                gap = max(1, width - left_visible - len(right))
                lines.append(
                    f"{key_part}{color}●{RESET} {age:>6}  {BOLD}{name_fmt}{RESET}"
                    f"  {path}{' ' * gap}{DIM}{right}{RESET}"
                )
                if r["status"] == "waiting" and r["waitingFor"]:
                    lines.append(f"{' ' * indent}{RED}⏸ esperando: {r['waitingFor']}{RESET}")
                if r.get("bg"):
                    lines.append(f"{' ' * indent}{GREEN}⚙{RESET} {DIM}agentes en "
                                 f"background activos{RESET}")
                if with_snippets and r["status"] in ("waiting", "idle"):
                    snippet = last_assistant_text(
                        r["cwd"], r["sessionId"], max_len=width - indent - 3
                    )
                    if snippet:
                        lines.append(f"{' ' * indent}{DIM}└ {snippet}{RESET}")
        if hidden:
            lines.append(f"{' ' * (1 + key_w)}{DIM}… +{hidden} sesiones más{RESET}")
        return lines, keymap

    lines, keymap = build(True, 0)
    if height and len(lines) + len(footer) + reserve + 1 > height:
        lines, keymap = build(False, 0)
        if len(lines) + len(footer) + reserve + 1 > height:
            lines, keymap = build(
                False, max(len(head), height - len(footer) - reserve - 2))
    return "\n".join(assemble(lines)), keymap


def term_size():
    s = shutil.get_terminal_size((110, 30))
    return min(s.columns, 200), s.lines


def watch_loop(interval):
    is_tty = sys.stdin.isatty()
    fd = sys.stdin.fileno() if is_tty else None
    old_attrs = None
    docker_detail = False
    usage_detail = False
    selected_pid = None
    mode = "list"       # "list" | "preview"
    preview_offset = 0
    reload_pending = False
    # auto-reload: si el binario en disco cambia (claudios update / brew
    # upgrade), el watch se re-ejecuta solo — el proceso viejo no se queda
    # corriendo código viejo
    script_path = os.path.realpath(__file__)
    try:
        script_mtime = os.path.getmtime(script_path)
    except OSError:
        script_mtime = None
    threading.Thread(target=slow_loop, daemon=True).start()

    def read_key():
        """Lee una tecla directo del fd (os.read evita el buffer de Python,
        que rompería el select); traduce flechas a 'up'/'down'."""
        try:
            ch = os.read(fd, 1).decode("utf-8", "ignore")
        except OSError:
            return ""
        if ch != "\x1b":
            return ch
        for expected in ("[", None):  # ESC [ A/B
            r, _, _ = select.select([fd], [], [], 0.03)
            if not r:
                return ch
            nxt = os.read(fd, 1).decode("utf-8", "ignore")
            if expected == "[":
                if nxt != "[":
                    return ch
            else:
                return {"A": "up", "B": "down",
                        "C": "right", "D": "left"}.get(nxt, ch)
        return ch
    # self-pipe: SIGWINCH (resize) despierta el select → redibujo inmediato
    rpipe, wpipe = os.pipe()
    os.set_blocking(wpipe, False)
    signal.set_wakeup_fd(wpipe, warn_on_full_buffer=False)
    signal.signal(signal.SIGWINCH, lambda signum, frame: None)
    sys.stdout.write("\x1b[?1049h\x1b[?25l")  # alt-screen + cursor oculto
    try:
        if is_tty:
            old_attrs = termios.tcgetattr(fd)
            raw = termios.tcgetattr(fd)
            raw[3] &= ~(termios.ICANON | termios.ECHO)
            termios.tcsetattr(fd, termios.TCSANOW, raw)
        while True:
            try:
                if (script_mtime
                        and os.path.getmtime(script_path) > script_mtime):
                    reload_pending = True
                    break
            except OSError:
                pass
            width, height = term_size()
            rows = collect()
            visible = {r["pid"] for r in rows}
            if selected_pid is not None and selected_pid not in visible:
                selected_pid = None  # la sesión seleccionada ya no existe
                mode = "list"
            sel_row = next((r for r in rows if r["pid"] == selected_pid), None)
            if mode == "preview" and sel_row:
                out, preview_offset = render_preview(
                    sel_row, width, height, preview_offset)
                keymap, order = {}, []
                hint = ("↑↓ scroll · Enter ir a su terminal · ←/Esc volver"
                        " · q salir")
            else:
                mode = "list"
                out, keymap = render(rows, width, height=height,
                                     show_keys=is_tty,
                                     docker_detail=docker_detail,
                                     usage_detail=usage_detail,
                                     selected_pid=selected_pid)
                order = list(keymap.values())  # filas visibles en orden
                hint = ("↑↓ mover · → ver · Enter ir · d docker · u uso"
                        " · q salir" if is_tty else "Ctrl+C para salir")
            sys.stdout.write("\x1b[2J\x1b[H")
            print(out)
            # sin newline final: si la última línea hace scroll se pierde el título
            print(f"\n{DIM} refresca cada {interval:g}s · {hint}{RESET}", end="")
            if SLOW["update"]:
                print(f"\n {YELLOW}⬆ v{SLOW['update']} disponible — "
                      f"corre `claudios update`{RESET}", end="")
            sys.stdout.flush()
            watch_fds = [fd, rpipe] if is_tty else [rpipe]
            r, _, _ = select.select(watch_fds, [], [], interval)
            if rpipe in r:
                os.read(rpipe, 1024)  # drenar el aviso de resize
                continue              # redibujar ya, sin esperar el tick
            if is_tty and fd in r:
                ch = read_key()
                if mode == "preview":
                    if ch in ("q", "Q", "left", "h", "H", "\x1b"):
                        mode = "list"
                        preview_offset = 0
                    elif ch in ("up", "k", "K"):
                        preview_offset += 3
                    elif ch in ("down", "j", "J"):
                        preview_offset = max(0, preview_offset - 3)
                    elif ch in ("\r", "\n") and sel_row:
                        tty2 = ttys_for([sel_row["pid"]]).get(sel_row["pid"])
                        focus_row(tty2, sel_row["cwd"], sel_row["pid"])
                    continue
                if ch in ("q", "Q"):
                    break
                if ch in ("d", "D"):
                    docker_detail = not docker_detail
                    continue
                if ch in ("u", "U"):
                    usage_detail = not usage_detail
                    continue
                if ch in ("right", "l", "L", " ") and order:
                    if selected_pid is None:
                        selected_pid = order[0]["pid"]
                    mode = "preview"
                    preview_offset = 0
                    continue
                if ch in ("up", "down", "j", "J", "k", "K") and order:
                    pids = [x["pid"] for x in order]
                    try:
                        i = pids.index(selected_pid)
                    except ValueError:
                        i = 0 if ch in ("down", "j", "J") else len(pids) - 1
                    else:
                        step = 1 if ch in ("down", "j", "J") else -1
                        i = (i + step) % len(pids)
                    selected_pid = pids[i]
                    continue
                if ch in ("\r", "\n") and selected_pid is not None:
                    sel = next((x for x in order
                                if x["pid"] == selected_pid), None)
                    if sel:
                        focus_row(sel["_tty"], sel["cwd"], sel["pid"])
                    continue
                row = keymap.get(ch)
                if row:
                    focus_row(row["_tty"], row["cwd"], row["pid"])
    except KeyboardInterrupt:
        pass
    finally:
        signal.set_wakeup_fd(-1)
        os.close(rpipe)
        os.close(wpipe)
        if old_attrs is not None:
            termios.tcsetattr(fd, termios.TCSANOW, old_attrs)
        sys.stdout.write("\x1b[?1049l\x1b[?25h")
        sys.stdout.flush()
    if reload_pending:
        # re-ejecutarse con la versión nueva, mismos argumentos
        os.execv(sys.executable, [sys.executable] + sys.argv)


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
        print(f"claudios v{__version__}")
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
        try:
            scan_usage(tail=512 * 1024)  # snapshot: tail corto, que sea rápido
            SLOW["usage"] = usage_summary()
        except Exception:
            pass
        print(render(collect(), term_size()[0], docker_detail=True,
                     usage_detail=True)[0])
    else:
        watch_loop(watch)


if __name__ == "__main__":
    main()
