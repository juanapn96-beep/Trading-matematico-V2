# CHANGELOG — ZAR ULTIMATE BOT v6

Registro de cambios del proyecto. Formato: `[Fecha Hora UTC] - [Módulo/Archivo] - Descripción - [Agente/Autor]`

---

## [2026-03-26 16:22 UTC] - FASE 0: Securización y Entorno

### config.py
- **Migración de credenciales a `.env`**: Las variables sensibles `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER`, `GEMINI_API_KEY`, `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`, `ALPHA_VANTAGE_KEY` y `FINNHUB_KEY` ya **no se almacenan como literales** en el código fuente. Ahora se cargan dinámicamente mediante `os.environ.get()` a través de `python-dotenv`.
- Añadido `import os`, `import sys` y `from dotenv import load_dotenv` con llamada a `load_dotenv()` al inicio del módulo.
- Añadida función `_require_env()` que valida la presencia de cada credencial obligatoria en el entorno y termina el proceso con un mensaje de error claro si alguna falta, evitando fallos silenciosos.
- Versión actualizada a `v6.5 — ENV SECURIZATION`.
- **[Agente: GitHub Copilot]**

### requirements.txt
- Añadida dependencia `python-dotenv>=1.0.0` para la carga del archivo `.env`.
- **[Agente: GitHub Copilot]**

### .env.example *(nuevo)*
- Creado archivo de plantilla con las variables de entorno requeridas y valores de ejemplo/placeholder.
- Los desarrolladores deben copiar este archivo a `.env` y rellenar con sus credenciales reales.
- **[Agente: GitHub Copilot]**

### .gitignore *(nuevo)*
- Creado archivo `.gitignore` que excluye:
  - `.env` — archivo de credenciales (nunca debe subirse al repo)
  - `__pycache__/` y archivos `*.pyc`/`*.pyo` — caché de bytecode Python
  - `*.db` y `memory/*.db` — base de datos SQLite de memoria del bot
  - `*.log` — archivos de log generados en ejecución
  - Directorios de entornos virtuales (`venv/`, `.venv/`, `env/`)
  - Artefactos de build y distribución
  - Archivos de IDEs (`.idea/`, `.vscode/`)
- **[Agente: GitHub Copilot]**

### CHANGELOG.md *(nuevo)*
- Creado archivo de trazabilidad de cambios en la raíz del repositorio.
- **[Agente: GitHub Copilot]**
