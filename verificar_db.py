import sqlite3, os
DB = r"C:\Users\USER\Desktop\Freshs_labors\database.db"  # misma ruta abs.
print("DB usada:", DB, "existe?", os.path.exists(DB))
con = sqlite3.connect(DB)
cur = con.cursor()
cur.execute("SELECT name FROM sqlite_master WHERE type='table';")
print("Tablas:", [r[0] for r in cur.fetchall()])
try:
    cur.execute("PRAGMA table_info(tarea);")
    cols = [f"{r[1]} {r[2]}" for r in cur.fetchall()]
    print("Columnas de tarea:", cols)
except Exception as e:
    print("Error consultando tarea:", e)
con.close()
