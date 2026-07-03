# claude-monitor (Claudios)

> Un solo comando para saber qué están haciendo **todos** tus Claude Code: quién trabaja, quién terminó, quién está bloqueado esperándote — y saltar a su pestaña con una tecla.

[🇬🇧 English](README.md) · 🇪🇸 Español

[![CI](https://github.com/rotorrest/claude-monitor/actions/workflows/ci.yml/badge.svg)](https://github.com/rotorrest/claude-monitor/actions/workflows/ci.yml)
[![CodeQL](https://github.com/rotorrest/claude-monitor/actions/workflows/codeql.yml/badge.svg)](https://github.com/rotorrest/claude-monitor/actions/workflows/codeql.yml)
[![Release](https://img.shields.io/github/v/release/rotorrest/claude-monitor)](https://github.com/rotorrest/claude-monitor/releases/latest)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![macOS](https://img.shields.io/badge/macOS-Apple%20Silicon%20%7C%20Intel-lightgrey)
![deps](https://img.shields.io/badge/dependencias-0%20(stdlib)-brightgreen)

<p align="center">
  <img src="docs/demo.svg" alt="claude-monitor mostrando sesiones de Claude Code agrupadas por atención" width="900">
</p>

⭐ **Si claude-monitor te ahorra alt-tabs, una estrellita ayuda a que otros lo encuentren.**

Corres 5+ sesiones de Claude Code en paralelo y vives alt-tabeando para ver cuál te necesita. `claudios` lee el estado que Claude Code publica en `~/.claude/sessions/` y te lo muestra ordenado por atención: primero lo bloqueado, después lo parado (con el último mensaje de Claude, para saber *en qué quedó*), al final lo que sigue trabajando. Presionas la tecla de la fila y te planta en esa pestaña de Terminal/iTerm2.

## Instalación

```bash
curl -fsSL https://raw.githubusercontent.com/rotorrest/claude-monitor/main/install.sh | bash
```

o con Homebrew:

```bash
brew install rotorrest/tap/claude-monitor
```

Si Homebrew se queja de un tap no confiable, corre `brew trust rotorrest/tap` y reintenta.

El installer verifica los SHA256 del release, instala `claudios` + `claude-notify` en `~/.local/bin` y crea el alias `claude-monitor`. Cero dependencias: Python 3 de sistema y ya.

## Uso

```bash
claudios                  # snapshot de todas las sesiones
claudios -w [seg]         # modo watch (refresca cada N seg, default 3)
claude-monitor           # alias: arranca directo en modo watch
claudios focus <id>       # enfoca la pestaña de esa sesión (sessionId o pid)
claudios --json           # snapshot en JSON, para scripts/statuslines
claudios update           # auto-update desde el último release (verifica SHA256)
```

### Teclas en modo watch

| Tecla | Acción |
|-------|--------|
| `↑`/`↓` o `j`/`k` | mover el cursor de selección entre sesiones |
| `→` o `l` | abrir la conversación de la sesión seleccionada, en vivo, sin salir de la terminal (`↑↓` scroll, `←`/`Esc` volver) |
| `Enter` | saltar a la sesión seleccionada, cruzando Spaces/pantallas |
| `1-9`, `a`… | saltar directo a esa fila (teclas rápidas) |
| `d` | detalle de Docker por contenedor |
| `u` | detalle de uso de tokens y costo por sesión |
| `q` | salir |

El salto sabe dónde vive cada sesión: pestaña exacta en **Terminal/iTerm2** (por tty), ventana del proyecto en **Cursor/VS Code/Windsurf** (detecta el editor por árbol de procesos y reutiliza su ventana con `open -a`). Hasta donde sabemos, ningún otro monitor salta a terminales integradas de editores.

### Qué muestra

- **Sesiones agrupadas por atención**: bloqueadas esperando permiso → paradas esperando tu respuesta (con el último mensaje de Claude) → trabajando.
- **Agentes en background**: si una sesión terminó su turno pero sus subagentes siguen escribiendo al transcript, se marca `⚙ agentes en background activos` en vez de aparecer como parada.
- **Métricas del sistema**: CPU, load, RAM + presión de memoria, swap, disco, Docker por contenedor, batería + temperatura y throttling térmico (para saber si tu Mac aguanta una sesión más).
- **Uso de tokens y costo (últimas 5h)**: totales in/out/cache, costo API equivalente estimado, burn rate con sparkline de 10 minutos, y top por sesión (tecla `u`) — leído de tus JSONL locales, cero red, con los precios del lineup actual de modelos.

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

`claudios --json` emite el estado crudo para componer con statuslines, SwiftBar/xbar, o lo que quieras:

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

- `claudios update` descarga el último release de GitHub, **verifica cada archivo contra `SHA256SUMS`** y se reemplaza atómicamente. Los watch corriendo detectan el binario nuevo y **se reinician solos en segundos** — no quedan monitores viejos.
- En modo watch revisa (máximo una vez al día) si hay versión nueva y lo avisa en el pie de pantalla.
- `CLAUDIOS_NO_UPDATE_CHECK=1` apaga el chequeo. Si instalaste por brew, puedes preferir `brew upgrade claude-monitor`.

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
- [x] Tokens/costo por sesión + burn rate desde los JSONL (inspirado en [ccusage](https://github.com/ryoppippi/ccusage) y [Claude-Code-Usage-Monitor](https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor))
- [ ] % de contexto restante por sesión ([claude-tui](https://github.com/slima4/claude-tui))
- [ ] CPU/RAM por sesión vía árbol de procesos ([claude-dashboard](https://github.com/seunggabi/claude-dashboard))
- [ ] Layout compacto automático en terminales angostas (ccusage)
- [ ] Web UI móvil con QR + aprobación remota de permisos (claude-code-monitor) — la joya de la corona


## FAQ / Problemas comunes

**¿Manda mi código o transcripts a algún lado?** No. Todo se lee localmente; la única llamada de red es el chequeo de versión diario a `api.github.com` (se apaga con `CLAUDIOS_NO_UPDATE_CHECK=1`).

**La tecla no salta a la sesión.** El salto usa AppleScript/System Events — dale permiso de **Accesibilidad** a tu terminal (Ajustes → Privacidad y seguridad → Accesibilidad) la primera vez que macOS lo pida. Para Cursor/VS Code la carpeta del proyecto debe estar abierta como raíz de la ventana.

**No aparecen sesiones.** El monitor lee `~/.claude/sessions/`, que las versiones recientes de Claude Code mantienen — actualiza Claude Code si ese directorio está vacío con sesiones corriendo.

**`brew install` dice que el tap no es confiable.** Corre `brew trust rotorrest/tap` y reintenta.

**El clic en la notificación no enfoca la pestaña.** Instala [terminal-notifier](https://github.com/julienXX/terminal-notifier) (`brew install terminal-notifier`); sin él las notificaciones caen a osascript plano (sin acción al clic).

## Herramientas relacionadas

| Herramienta | Enfoque |
|---|---|
| [ccusage](https://github.com/ryoppippi/ccusage) | costos y tokens desde los JSONL |
| [Claude-Code-Usage-Monitor](https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor) | límites de plan, burn rate, predicción |
| [claude-squad](https://github.com/smtg-ai/claude-squad) | orquestar agentes en tmux + worktrees |
| [ccboard](https://github.com/FlorianBruniaux/ccboard) | dashboard todo-en-uno (Rust) |
| **claude-monitor** | **¿quién necesita mi atención ahora y en qué quedó?** |

## Desarrollo

```bash
git clone https://github.com/rotorrest/claude-monitor && cd claude-monitor
python3 src/claudios.py -w        # correr desde el fuente
ruff check src/ && bandit -ll -r src/
```

Los PRs corren el mismo CI (lint, SAST, CodeQL, smoke test en macOS). Para publicar: bump de `__version__` en `src/claudios.py`, tag `vX.Y.Z`, push del tag — el pipeline hace el resto (gate de seguridad → build → release con checksums → bump de la fórmula de brew).

## Desinstalar

```bash
rm ~/.local/bin/claudios ~/.local/bin/claude-notify ~/.local/bin/claude-monitor
# o: brew uninstall claude-monitor
```


## Star history

[![Star History Chart](https://api.star-history.com/svg?repos=rotorrest/claude-monitor&type=Date)](https://star-history.com/#rotorrest/claude-monitor&Date)

## Licencia

[MIT](LICENSE)
