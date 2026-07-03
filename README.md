# claudes · claude-monitor

> Un solo comando para saber qué están haciendo **todos** tus Claude Code: quién trabaja, quién terminó, quién está bloqueado esperándote — y saltar a su pestaña con una tecla.

[![CI](https://github.com/rotorrest/claudes/actions/workflows/ci.yml/badge.svg)](https://github.com/rotorrest/claudes/actions/workflows/ci.yml)
[![CodeQL](https://github.com/rotorrest/claudes/actions/workflows/codeql.yml/badge.svg)](https://github.com/rotorrest/claudes/actions/workflows/codeql.yml)
[![Release](https://img.shields.io/github/v/release/rotorrest/claudes)](https://github.com/rotorrest/claudes/releases/latest)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![macOS](https://img.shields.io/badge/macOS-Apple%20Silicon%20%7C%20Intel-lightgrey)
![deps](https://img.shields.io/badge/dependencias-0%20(stdlib)-brightgreen)

```text
 CLAUDE MONITOR · 5 sesiones                                          23:08:01
 ──────────────────────────────────────────────────────────────────────────────
 CPU  13% █░░░░░░░░░ load 4.1/18        RAM 33.1G/48G ███████░░░ presión normal
 DOCKER 4 cont · cpu 0.2% · ram 290M    BAT 100% AC · 30.7°C   SSD 730G libres
 ──────────────────────────────────────────────────────────────────────────────

   ▍ESPERAN TU ACCIÓN (1)
 1 ●    4m  api-pagos        ~/work/api-pagos                    s003 · 41231
            ⏸ esperando: permiso para Bash(git push)

   ▍PARADAS · esperan tu respuesta (2)
 2 ●   32m  blog             ~/proyectos/blog                    s007 · 38112
            └ Listo el post. ¿Lo publico o quieres revisar el borrador antes?
 3 ●    2m  scraper          ~/work/scraper                      s012 · 44903
            └ Los tests pasan. Quedó pendiente decidir el formato del export.

   ▍TRABAJANDO (2)
 4 ●   18m  data-pipeline    ~/work/etl                          s001 · 39557
            ⚙ agentes en background activos
 5 ●    1m  frontend         ~/work/webapp                       s009 · 45120

 1 esperan tu acción · 2 paradas · 2 trabajando
```

Corres 5+ sesiones de Claude Code en paralelo y vives alt-tabeando para ver cuál te necesita. `claudes` lee el estado que Claude Code publica en `~/.claude/sessions/` y te lo muestra ordenado por atención: primero lo bloqueado, después lo parado (con el último mensaje de Claude, para saber *en qué quedó*), al final lo que sigue trabajando. Presionas la tecla de la fila y te planta en esa pestaña de Terminal/iTerm2.

## Instalación

```bash
curl -fsSL https://raw.githubusercontent.com/rotorrest/claudes/main/install.sh | bash
```

o con Homebrew:

```bash
brew install rotorrest/tap/claudes
```

El installer verifica los SHA256 del release, instala `claudes` + `claude-notify` en `~/.local/bin` y crea el alias `claude-monitor`. Cero dependencias: Python 3 de sistema y ya.

## Uso

```bash
claudes                  # snapshot de todas las sesiones
claudes -w [seg]         # modo watch (refresca cada N seg, default 3)
claude-monitor           # alias: arranca directo en modo watch
claudes focus <id>       # enfoca la pestaña de esa sesión (sessionId o pid)
claudes --json           # snapshot en JSON, para scripts/statuslines
claudes update           # auto-update desde el último release (verifica SHA256)
```

### Teclas en modo watch

| Tecla | Acción |
|-------|--------|
| `1-9`, `a`… | saltar a esa sesión, cruzando Spaces/pantallas |
| `d` | detalle de Docker por contenedor |
| `q` | salir |

El salto sabe dónde vive cada sesión: pestaña exacta en **Terminal/iTerm2** (por tty), ventana del proyecto en **Cursor/VS Code/Windsurf** (detecta el editor por árbol de procesos y reutiliza su ventana con `open -a`). Hasta donde sabemos, ningún otro monitor salta a terminales integradas de editores.

### Qué muestra

- **Sesiones agrupadas por atención**: bloqueadas esperando permiso → paradas esperando tu respuesta (con el último mensaje de Claude) → trabajando.
- **Agentes en background**: si una sesión terminó su turno pero sus subagentes siguen escribiendo al transcript, se marca `⚙ agentes en background activos` en vez de aparecer como parada.
- **Métricas del sistema**: CPU, load, RAM + presión de memoria, swap, disco, Docker por contenedor, batería + temperatura y throttling térmico (para saber si tu Mac aguanta una sesión más).

## Notificaciones

`claude-notify` convierte los hooks `Stop`/`Notification` de Claude Code en notificaciones nativas de macOS. Con [terminal-notifier](https://github.com/julienXX/terminal-notifier) instalado (`brew install terminal-notifier`), **hacer clic en la notificación te lleva a la pestaña de esa sesión**.

Agrega a `~/.claude/settings.json` (fusiona con tus hooks existentes, no los reemplaces):

```json
{
  "hooks": {
    "Stop": [
      { "hooks": [{ "type": "command", "command": "~/.local/bin/claude-notify" }] }
    ],
    "Notification": [
      { "hooks": [{ "type": "command", "command": "~/.local/bin/claude-notify" }] }
    ]
  }
}
```

## Modo JSON

`claudes --json` emite el estado crudo para componer con statuslines, SwiftBar/xbar, o lo que quieras:

```json
[
  {
    "pid": 41231,
    "name": "api-pagos",
    "cwd": "/Users/tu/work/api-pagos",
    "sessionId": "1fab6f1a-…",
    "status": "waiting",
    "bg": false,
    "age": 254.3,
    "waitingFor": "permiso para Bash(git push)"
  }
]
```

## Auto-update

- `claudes update` descarga el último release de GitHub, **verifica cada archivo contra `SHA256SUMS`** y se reemplaza atómicamente.
- En modo watch revisa (máximo una vez al día) si hay versión nueva y lo avisa en el pie de pantalla.
- `CLAUDES_NO_UPDATE_CHECK=1` apaga el chequeo. Si instalaste por brew, puedes preferir `brew upgrade claudes`.

## Seguridad y privacidad

- **Todo es local.** La herramienta lee archivos de tu `~/.claude/` y comandos de sistema (`ps`, `vm_stat`, `pmset`…). La única llamada de red es el chequeo de versión a `api.github.com` (1/día, opt-out arriba).
- **Cero dependencias.** Python stdlib puro — no hay supply chain que auditar, y el CI lo verifica con una guardia `stdlib-only` que falla si algún import sale de la stdlib.
- **Entradas saneadas.** Los strings que vienen de archivos de sesión/transcripts se limpian de caracteres de control antes de renderizar (nada de inyección de escapes ANSI en tu terminal), los IDs de sesión se validan antes de usarse en rutas o comandos, y los tty se validan antes de interpolarse en AppleScript.
- **Pipeline con dientes.** Cada push corre `ruff` + `bandit` + CodeQL + shellcheck + la guardia stdlib; cada release pasa el mismo gate **antes** de publicarse, y el tag debe coincidir con `__version__`.

## Roadmap (features robadas con orgullo)

Ideas mapeadas de las mejores herramientas del ecosistema — crédito donde corresponde:

- [x] Orden attention-first (validado por [tmux-claude-session-manager](https://github.com/craftzdog/tmux-claude-session-manager))
- [x] `--json` para componer con otras herramientas ([Claude-Code-Usage-Monitor](https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor))
- [x] Detección de agentes en background en sesiones "idle"
- [x] Salto a terminales integradas de editores (Cursor/VS Code/Windsurf) cruzando Spaces
- [ ] Notificaciones con debounce, duración del turno y supresión si ya estás mirando esa pestaña ([claude-ghostty-notify](https://github.com/thejustinwalsh/claude-ghostty-notify), [CCNotify](https://github.com/dazuiba/CCNotify))
- [ ] Salto por tty en Ghostty (hoy solo fallback por título en el Space actual) ([claude-code-monitor](https://github.com/onikan27/claude-code-monitor))
- [ ] Tokens/costo por sesión + burn rate desde los JSONL ([ccusage](https://github.com/ryoppippi/ccusage))
- [ ] % de contexto restante por sesión ([claude-tui](https://github.com/slima4/claude-tui))
- [ ] CPU/RAM por sesión vía árbol de procesos ([claude-dashboard](https://github.com/seunggabi/claude-dashboard))
- [ ] Layout compacto automático en terminales angostas (ccusage)
- [ ] Web UI móvil con QR + aprobación remota de permisos (claude-code-monitor) — la joya de la corona

## Herramientas relacionadas

| Herramienta | Enfoque |
|---|---|
| [ccusage](https://github.com/ryoppippi/ccusage) | costos y tokens desde los JSONL |
| [Claude-Code-Usage-Monitor](https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor) | límites de plan, burn rate, predicción |
| [claude-squad](https://github.com/smtg-ai/claude-squad) | orquestar agentes en tmux + worktrees |
| [ccboard](https://github.com/FlorianBruniaux/ccboard) | dashboard todo-en-uno (Rust) |
| **claudes** | **¿quién necesita mi atención ahora y en qué quedó?** |

## Desarrollo

```bash
git clone https://github.com/rotorrest/claudes && cd claudes
python3 src/claudes.py -w        # correr desde el fuente
ruff check src/ && bandit -ll -r src/
```

Los PRs corren el mismo CI (lint, SAST, CodeQL, smoke test en macOS). Para publicar: bump de `__version__` en `src/claudes.py`, tag `vX.Y.Z`, push del tag — el pipeline hace el resto (gate de seguridad → build → release con checksums → bump de la fórmula de brew).

## Desinstalar

```bash
rm ~/.local/bin/claudes ~/.local/bin/claude-notify ~/.local/bin/claude-monitor
# o: brew uninstall claudes
```

## Licencia

[MIT](LICENSE)
