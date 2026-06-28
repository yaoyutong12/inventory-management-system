"""Import supplier data and start the app"""
import sys
sys.path.insert(0, r'C:\Users\Administrator\WorkBuddy\2026-06-27-23-22-25\inventory-system')

from app import app, init_db, DB_PATH
import sqlite3
import pandas as pd

# Initialize DB
init_db()
print("Database initialized.")

# Import supplier data
xlsx_path = r'C:\Users\Administrator\WorkBuddy\2026-06-27-23-22-25\supplier_list.xlsx'
df = pd.read_excel(xlsx_path, dtype=str)
df.columns = [c.strip() for c in df.columns]

db = sqlite3.connect(str(DB_PATH))
db.execute("DELETE FROM supplier_items")
db.execute("DELETE FROM supplier_imports")

cur = db.execute("INSERT INTO supplier_imports (filename, total_items) VALUES (?, 0)", (xlsx_path.split('\\')[-1],))
import_id = cur.lastrowid

col_map = {'跟踪号': 'tracking_no', '库位号': 'location_code', '包裹重量': 'weight',
           '包裹尺寸': 'dimensions', '货品名称': 'product_name', '预报数量': 'expected_qty',
           '品名': 'category'}

count = 0
for _, row in df.iterrows():
    vals = {}
    for cn, en in col_map.items():
        val = row.get(cn, '')
        if pd.isna(val) or str(val).strip() == '':
            vals[en] = None
        else:
            vals[en] = str(val).strip()
    try:
        vals['weight'] = float(vals['weight']) if vals['weight'] else None
    except (ValueError, TypeError):
        vals['weight'] = None
    try:
        vals['expected_qty'] = int(float(vals['expected_qty'])) if vals['expected_qty'] else 1
    except (ValueError, TypeError):
        vals['expected_qty'] = 1

    db.execute("""
        INSERT INTO supplier_items (import_id, tracking_no, location_code, weight, dimensions, product_name, expected_qty, category)
        VALUES (?,?,?,?,?,?,?,?)
    """, (import_id, vals['tracking_no'], vals['location_code'], vals['weight'],
          vals['dimensions'], vals['product_name'], vals['expected_qty'], vals['category']))
    count += 1

db.execute("UPDATE supplier_imports SET total_items=? WHERE id=?", (count, import_id))
db.commit()
db.close()
print(f"Imported {count} items from supplier list.")

# Start Flask
print("\nStarting server at http://localhost:5000")
app.run(debug=False, host='0.0.0.0', port=5000)
