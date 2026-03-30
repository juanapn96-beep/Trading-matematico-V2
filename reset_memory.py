"""
reset_memory.py — Reinicia la memoria del bot completamente.

Hace un backup automático antes de borrar, por seguridad.
Ejecutar con: python reset_memory.py

USO:
    python reset_memory.py              # Solicita confirmación interactiva
    python reset_memory.py --confirm    # Sin confirmación (para scripts)
"""

import os
import sys
import shutil
import sqlite3
from datetime import datetime


MEMORY_DIR = os.path.join(os.path.dirname(__file__), "memory")
DB_PATH    = os.path.join(MEMORY_DIR, "zar_memory.db")
BACKUP_DIR = MEMORY_DIR

CONFIRM_STRING = "SI"


def _backup_db(db_path: str) -> str:
    """Crea un backup con timestamp de la base de datos. Retorna la ruta del backup."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(BACKUP_DIR, f"zar_memory_backup_{ts}.db")
    shutil.copy2(db_path, backup_path)
    return backup_path


def _count_trades(db_path: str) -> int:
    """Retorna el número de trades almacenados en la DB."""
    try:
        con = sqlite3.connect(db_path)
        cur = con.execute("SELECT COUNT(*) FROM trades")
        count = cur.fetchone()[0]
        con.close()
        return count
    except Exception:
        return 0


def main() -> None:
    auto_confirm = "--confirm" in sys.argv

    print("=" * 60)
    print("  ZAR ULTIMATE BOT v6 — Reset de Memoria")
    print("=" * 60)

    if not os.path.exists(DB_PATH):
        print(f"\n✅  No existe la base de datos en:\n    {DB_PATH}")
        print("    No hay nada que borrar. El bot creará una DB vacía al iniciar.")
        return

    trade_count = _count_trades(DB_PATH)
    db_size_kb  = os.path.getsize(DB_PATH) / 1024

    print(f"\n📊  Base de datos encontrada:")
    print(f"    Ruta  : {DB_PATH}")
    print(f"    Tamaño: {db_size_kb:.1f} KB")
    print(f"    Trades: {trade_count}")

    if not auto_confirm:
        print(
            "\n⚠️   ATENCIÓN: Esta acción borrará TODA la memoria del bot.\n"
            "    Se creará un backup automático antes de continuar.\n"
        )
        answer = input(f"    ¿Confirmas el reset? (escribe '{CONFIRM_STRING}' para confirmar): ").strip()
        if answer.upper() != CONFIRM_STRING:
            print("\n❌  Reset cancelado por el usuario.")
            return

    # Crear backup
    backup_path = _backup_db(DB_PATH)
    print(f"\n💾  Backup guardado en:\n    {backup_path}")

    # Borrar la DB
    os.remove(DB_PATH)
    print(f"\n🗑️   Base de datos eliminada: {DB_PATH}")
    print("\n✅  Reset completado exitosamente.")
    print("    El bot creará una nueva DB vacía al próximo inicio.")
    print(f"    Si necesitas recuperar los datos, restaura desde:\n    {backup_path}")


if __name__ == "__main__":
    main()
