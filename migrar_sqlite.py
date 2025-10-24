from pathlib import Path
import sqlite3, shutil, argparse, sys, datetime

BASEDIR = Path(__file__).resolve().parent
DB_PATH = BASEDIR / "data" / "database.db"
BACKUPS_DIR = BASEDIR / "data" / "backups"
BACKUPS_DIR.mkdir(parents=True, exist_ok=True)

def backup_db():
    if not DB_PATH.exists():
        print(f"[migrar_sqlite] No existe {DB_PATH}")
        sys.exit(1)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = BACKUPS_DIR / f"database.{stamp}.bak"
    shutil.copy2(DB_PATH, dest)
    print(f"[migrar_sqlite] Backup: {dest}")

def add_col(cur, table, col, coldef):
    cur.execute(f"PRAGMA table_info({table})")
    if col not in [r[1] for r in cur.fetchall()]:
        print(f" + {table}.{col} {coldef}")
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coldef}")

def add_idx(cur, name, table, cols):
    cur.execute("SELECT name FROM sqlite_master WHERE type='index' AND name=?", (name,))
    if not cur.fetchone():
        print(f" + INDEX {name} ON {table}({', '.join(cols)})")
        cur.execute(f"CREATE INDEX {name} ON {table} ({', '.join(cols)})")

def run():
    backup_db()
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    # Crear tabla turno si no existe
    cur.execute("""
    CREATE TABLE IF NOT EXISTS turno (
        id INTEGER PRIMARY KEY,
        tarea_id INTEGER NOT NULL,
        numero INTEGER NOT NULL,
        hora_inicio TEXT NOT NULL,
        hora_fin TEXT NOT NULL,
        FOREIGN KEY(tarea_id) REFERENCES tarea(id)
    );
    """)

    # columnas en asignacion
    add_col(cur, "asignacion", "turno_num", "INTEGER")
    add_col(cur, "asignacion", "doble_turno", "INTEGER DEFAULT 0")

    # Usuario: datos de pago
    add_col(cur, "Usuario", "zelle_nombre", "TEXT")
    add_col(cur, "Usuario", "zelle_cuenta", "TEXT")
    add_col(cur, "Usuario", "pago_ciclo", "TEXT DEFAULT 'semana_en_canje'")

    # Tarea: exigencias
    add_col(cur, "Tarea", "exigencias_text", "TEXT")

    # Reporte: flag de confirmación (primer reporte)
    add_col(cur, "Reporte", "tipo_especial", "TEXT")  # null|confirmacion

    # Asignacion: estado para bloquear/eliminar
    add_col(cur, "Asignacion", "estado", "TEXT DEFAULT 'activa'")

    # Índices útiles
    add_idx(cur, "idx_asig_user_task", "Asignacion", ["usuario_id", "tarea_id"])
    add_idx(cur, "idx_rep_asig_tipo", "Reporte", ["tarea_id", "usuario_id", "tipo"])

    con.commit()
    con.execute("VACUUM")
    con.close()
    print("[migrar_sqlite] OK")

if __name__ == "__main__":
    run()
