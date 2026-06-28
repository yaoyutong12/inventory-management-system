# -*- coding: utf-8 -*-
"""
托盘杂货进销存管理系统
Pallet Goods Inventory Management System
"""
import os
import io
import json
import uuid
import base64
import sqlite3
from datetime import datetime
from pathlib import Path

from flask import Flask, request, jsonify, render_template, send_file, g
import pandas as pd
import qrcode
from PIL import Image
from pyzbar.pyzbar import decode as pyzbar_decode
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

try:
    import google.generativeai as genai
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False
    genai = None

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024
BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / 'data' / 'inventory.db'
UPLOAD_DIR = BASE_DIR / 'uploads'

for d in [BASE_DIR / 'data', UPLOAD_DIR]:
    d.mkdir(exist_ok=True)


# ─── Database ───────────────────────────────────────────────────
def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(str(DB_PATH))
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


@app.teardown_appcontext
def close_db(e):
    db = g.pop('db', None)
    if db:
        db.close()


def init_db():
    db = sqlite3.connect(str(DB_PATH))
    db.executescript("""
        CREATE TABLE IF NOT EXISTS supplier_imports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT,
            import_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            total_items INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS supplier_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            import_id INTEGER REFERENCES supplier_imports(id),
            tracking_no TEXT,
            location_code TEXT,
            weight REAL,
            dimensions TEXT,
            product_name TEXT,
            expected_qty INTEGER DEFAULT 1,
            category TEXT,
            unit_cost REAL,
            matched BOOLEAN DEFAULT 0,
            matched_date TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            internal_code TEXT UNIQUE NOT NULL,
            supplier_item_id INTEGER REFERENCES supplier_items(id),
            product_name TEXT,
            product_name_ja TEXT,
            category TEXT,
            weight REAL,
            dimensions TEXT,
            unit_cost REAL,
            selling_price REAL,
            is_high_value INTEGER DEFAULT 0,
            high_value_reason TEXT,
            status TEXT DEFAULT 'in_stock',
            scrap_reason TEXT,
            scrapped_at TIMESTAMP,
            location TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS inbound_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER REFERENCES products(id),
            supplier_item_id INTEGER REFERENCES supplier_items(id),
            qty INTEGER DEFAULT 1,
            unit_cost REAL,
            inbound_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            notes TEXT
        );
        CREATE TABLE IF NOT EXISTS sales_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER REFERENCES products(id),
            qty INTEGER DEFAULT 1,
            unit_price REAL,
            total_amount REAL,
            cost_amount REAL,
            profit_amount REAL,
            payment_method TEXT DEFAULT 'cash',
            platform TEXT,
            sale_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            notes TEXT
        );
        CREATE TABLE IF NOT EXISTS mercari_listings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER REFERENCES products(id),
            title TEXT,
            description TEXT,
            price INTEGER,
            shipping_method TEXT,
            condition TEXT,
            status TEXT DEFAULT 'draft',
            listing_url TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            posted_at TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS receipts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sale_id INTEGER REFERENCES sales_records(id),
            receipt_no TEXT UNIQUE,
            receipt_html TEXT,
            printed_count INTEGER DEFAULT 0,
            last_printed TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_supplier_items_tracking ON supplier_items(tracking_no);
        CREATE INDEX IF NOT EXISTS idx_products_internal ON products(internal_code);
        CREATE INDEX IF NOT EXISTS idx_supplier_items_import ON supplier_items(import_id);
    """)
    
    # Migrate: add scrap columns if not exist
    try:
        db.execute("ALTER TABLE products ADD COLUMN scrap_reason TEXT")
    except:
        pass
    try:
        db.execute("ALTER TABLE products ADD COLUMN scrapped_at TIMESTAMP")
    except:
        pass
    
    db.commit()
    db.close()


# ─── Helpers ────────────────────────────────────────────────────
def generate_internal_code():
    return 'PLT-' + uuid.uuid4().hex[:8].upper()


def generate_qrcode_base64(data):
    qr = qrcode.QRCode(version=1, box_size=4, border=2)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color='black', back_color='white')
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    import base64
    return base64.b64encode(buf.getvalue()).decode()


# ─── Routes: Pages ──────────────────────────────────────────────
@app.route('/')
def index():
    db = get_db()
    total_products = db.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    total_in_stock = db.execute("SELECT COUNT(*) FROM products WHERE status='in_stock'").fetchone()[0]
    total_sold = db.execute("SELECT COUNT(*) FROM sales_records").fetchone()[0]
    total_revenue = db.execute("SELECT COALESCE(SUM(total_amount),0) FROM sales_records").fetchone()[0]
    total_profit = db.execute("SELECT COALESCE(SUM(profit_amount),0) FROM sales_records").fetchone()[0]
    recent_sales = db.execute("""
        SELECT s.*, p.product_name, p.internal_code
        FROM sales_records s LEFT JOIN products p ON s.product_id = p.id
        ORDER BY s.sale_date DESC LIMIT 10
    """).fetchall()
    low_stock = db.execute("""
        SELECT p.*, 
            (SELECT COALESCE(SUM(ir.qty),0) FROM inbound_records ir WHERE ir.product_id=p.id) as total_in,
            (SELECT COALESCE(SUM(sr.qty),0) FROM sales_records sr WHERE sr.product_id=p.id) as total_out
        FROM products p WHERE p.status='in_stock'
    """).fetchall()
    
    stock_data = []
    for row in low_stock:
        stock = row['total_in'] - row['total_out']
        if stock <= 5:
            stock_data.append({**dict(row), 'current_stock': stock})
    
    return render_template('index.html',
        total_products=total_products, total_in_stock=total_in_stock,
        total_sold=total_sold, total_revenue=total_revenue,
        total_profit=total_profit, recent_sales=recent_sales,
        low_stock=stock_data)


@app.route('/import')
def import_page():
    db = get_db()
    imports = db.execute("SELECT * FROM supplier_imports ORDER BY import_date DESC").fetchall()
    return render_template('import.html', imports=imports)


@app.route('/inbound')
def inbound_page():
    return render_template('inbound.html')


@app.route('/sales')
def sales_page():
    return render_template('sales.html')


@app.route('/inventory')
def inventory_page():
    return render_template('inventory.html')


@app.route('/labels')
def labels_page():
    return render_template('labels.html')


@app.route('/mercari')
def mercari_page():
    return render_template('mercari.html')


@app.route('/guide')
def guide_page():
    return render_template('guide.html')


@app.route('/report')
def report_page():
    return render_template('report.html')


# ─── API: Supplier Import ───────────────────────────────────────
@app.route('/api/import/upload', methods=['POST'])
def api_import_upload():
    file = request.files.get('file')
    if not file:
        return jsonify({'error': 'ファイルがありません'}), 400
    
    filename = file.filename
    db = get_db()
    cur = db.execute("INSERT INTO supplier_imports (filename) VALUES (?)", (filename,))
    import_id = cur.lastrowid
    
    df = pd.read_excel(file, dtype=str)
    df.columns = [c.strip() for c in df.columns]
    
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
    
    return jsonify({'success': True, 'import_id': import_id, 'total_items': count})


@app.route('/api/import/<int:import_id>/items')
def api_import_items(import_id):
    db = get_db()
    items = db.execute("""
        SELECT * FROM supplier_items WHERE import_id=? ORDER BY id
    """, (import_id,)).fetchall()
    return jsonify([dict(row) for row in items])


@app.route('/api/imports')
def api_imports():
    db = get_db()
    imports = db.execute("SELECT * FROM supplier_imports ORDER BY import_date DESC").fetchall()
    return jsonify([dict(row) for row in imports])


# ─── API: Barcode Lookup ────────────────────────────────────────
@app.route('/api/lookup/<barcode>')
def api_lookup(barcode):
    db = get_db()
    # First check internal products (exclude scrapped)
    product = db.execute("SELECT * FROM products WHERE internal_code=? AND status != 'scrapped'", (barcode,)).fetchone()
    if product:
        result = dict(product)
        # Get stock info
        total_in = db.execute("SELECT COALESCE(SUM(qty),0) FROM inbound_records WHERE product_id=?", (product['id'],)).fetchone()[0]
        total_out = db.execute("SELECT COALESCE(SUM(qty),0) FROM sales_records WHERE product_id=?", (product['id'],)).fetchone()[0]
        result['current_stock'] = total_in - total_out
        result['type'] = 'product'
        return jsonify(result)
    
    # Check supplier items
    item = db.execute("SELECT * FROM supplier_items WHERE tracking_no=?", (barcode,)).fetchone()
    if item:
        result = dict(item)
        result['type'] = 'supplier_item'
        return jsonify(result)
    
    return jsonify({'error': '見つかりません', 'type': 'unknown'})


# ─── API: Inbound (入库) ────────────────────────────────────────
@app.route('/api/inbound/create', methods=['POST'])
def api_inbound_create():
    data = request.json
    supplier_item_id = data.get('supplier_item_id')
    unit_cost = data.get('unit_cost', 0)
    qty = data.get('qty', 1)
    is_high_value = data.get('is_high_value', False)
    selling_price = data.get('selling_price')
    location = data.get('location', '')
    inbound_date = data.get('inbound_date', '')  # 新增：可修改入库日期
    
    db = get_db()
    item = db.execute("SELECT * FROM supplier_items WHERE id=?", (supplier_item_id,)).fetchone()
    if not item:
        return jsonify({'error': '商品が見つかりません'}), 404
    
    # Check if already has a product
    existing = db.execute("SELECT * FROM products WHERE supplier_item_id=?", (supplier_item_id,)).fetchone()
    
    if existing:
        product_id = existing['id']
        if unit_cost and not existing['unit_cost']:
            db.execute("UPDATE products SET unit_cost=?, selling_price=?, is_high_value=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                      (unit_cost, selling_price or 0, 1 if is_high_value else 0, product_id))
    else:
        internal_code = generate_internal_code()
        cur = db.execute("""
            INSERT INTO products (internal_code, supplier_item_id, product_name, product_name_ja, category, weight, dimensions, unit_cost, selling_price, is_high_value, location)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (internal_code, supplier_item_id, item['product_name'], item['product_name'],
              item['category'], item['weight'], item['dimensions'],
              unit_cost or 0, selling_price or 0, 1 if is_high_value else 0, location))
        product_id = cur.lastrowid
    
    # Record inbound (支持自定义入库日期)
    if inbound_date:
        db.execute("INSERT INTO inbound_records (product_id, supplier_item_id, qty, unit_cost, inbound_date) VALUES (?,?,?,?,?)",
                  (product_id, supplier_item_id, qty, unit_cost or 0, inbound_date))
    else:
        db.execute("INSERT INTO inbound_records (product_id, supplier_item_id, qty, unit_cost) VALUES (?,?,?,?)",
                  (product_id, supplier_item_id, qty, unit_cost or 0))
    
    # Mark supplier item as matched
    db.execute("UPDATE supplier_items SET matched=1, matched_date=CURRENT_TIMESTAMP WHERE id=?", (supplier_item_id,))
    db.commit()
    
    product = db.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    qr_data = generate_qrcode_base64(product['internal_code'])
    
    return jsonify({
        'success': True,
        'product': dict(product),
        'qr_code': qr_data,
        'is_new': not bool(existing)
    })


@app.route('/api/inbound/unmatched')
def api_inbound_unmatched():
    db = get_db()
    items = db.execute("SELECT * FROM supplier_items WHERE matched=0 ORDER BY id").fetchall()
    return jsonify([dict(row) for row in items])


# ─── API: Sales (销售) ──────────────────────────────────────────
@app.route('/api/sales/create', methods=['POST'])
def api_sales_create():
    data = request.json
    product_id = data.get('product_id')
    qty = data.get('qty', 1)
    unit_price = data.get('unit_price', 0)
    payment_method = data.get('payment_method', 'cash')
    platform = data.get('platform', '')
    
    db = get_db()
    product = db.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    if not product:
        return jsonify({'error': '商品が見つかりません'}), 404
    
    # Check stock
    total_in = db.execute("SELECT COALESCE(SUM(qty),0) FROM inbound_records WHERE product_id=?", (product_id,)).fetchone()[0]
    total_out = db.execute("SELECT COALESCE(SUM(qty),0) FROM sales_records WHERE product_id=?", (product_id,)).fetchone()[0]
    current_stock = total_in - total_out
    if current_stock < qty:
        return jsonify({'error': f'在庫不足です。現在の在庫: {current_stock}個'}), 400
    
    total_amount = unit_price * qty
    cost_amount = (product['unit_cost'] or 0) * qty
    profit_amount = total_amount - cost_amount
    
    cur = db.execute("""
        INSERT INTO sales_records (product_id, qty, unit_price, total_amount, cost_amount, profit_amount, payment_method, platform)
        VALUES (?,?,?,?,?,?,?,?)
    """, (product_id, qty, unit_price, total_amount, cost_amount, profit_amount, payment_method, platform))
    
    # Check if still in stock
    new_stock = current_stock - qty
    if new_stock <= 0:
        db.execute("UPDATE products SET status='sold_out', updated_at=CURRENT_TIMESTAMP WHERE id=?", (product_id,))
    
    db.commit()
    
    sale_id = cur.lastrowid
    
    return jsonify({
        'success': True,
        'sale_id': sale_id,
        'product_name': product['product_name'],
        'internal_code': product['internal_code'],
        'qty': qty, 'unit_price': unit_price, 'total_amount': total_amount,
        'profit': profit_amount, 'new_stock': new_stock,
        'payment_method': payment_method
    })


# ─── API: Inventory ─────────────────────────────────────────────
@app.route('/api/inventory/list')
def api_inventory_list():
    db = get_db()
    products = db.execute("SELECT * FROM products ORDER BY updated_at DESC").fetchall()
    result = []
    for p in products:
        total_in = db.execute("SELECT COALESCE(SUM(qty),0) FROM inbound_records WHERE product_id=?", (p['id'],)).fetchone()[0]
        total_out = db.execute("SELECT COALESCE(SUM(qty),0) FROM sales_records WHERE product_id=?", (p['id'],)).fetchone()[0]
        d = dict(p)
        d['current_stock'] = total_in - total_out
        d['total_in'] = total_in
        d['total_out'] = total_out
        d['total_revenue'] = db.execute("SELECT COALESCE(SUM(total_amount),0) FROM sales_records WHERE product_id=?", (p['id'],)).fetchone()[0]
        result.append(d)
    return jsonify(result)


@app.route('/api/inventory/stats')
def api_inventory_stats():
    db = get_db()
    stats = {
        'total_products': db.execute("SELECT COUNT(*) FROM products").fetchone()[0],
        'in_stock': db.execute("SELECT COUNT(*) FROM products WHERE status='in_stock'").fetchone()[0],
        'sold_out': db.execute("SELECT COUNT(*) FROM products WHERE status='sold_out'").fetchone()[0],
        'total_revenue': db.execute("SELECT COALESCE(SUM(total_amount),0) FROM sales_records").fetchone()[0],
        'total_profit': db.execute("SELECT COALESCE(SUM(profit_amount),0) FROM sales_records").fetchone()[0],
        'total_sales_count': db.execute("SELECT COUNT(*) FROM sales_records").fetchone()[0],
        'high_value_count': db.execute("SELECT COUNT(*) FROM products WHERE is_high_value=1").fetchone()[0],
    }
    
    # By category
    categories = db.execute("""
        SELECT category, COUNT(*) as cnt, COALESCE(SUM(selling_price),0) as total_value
        FROM products GROUP BY category ORDER BY cnt DESC
    """).fetchall()
    stats['categories'] = [dict(row) for row in categories]
    
    # Monthly sales
    monthly = db.execute("""
        SELECT strftime('%Y-%m', sale_date) as month, 
               COUNT(*) as cnt, COALESCE(SUM(total_amount),0) as revenue,
               COALESCE(SUM(profit_amount),0) as profit
        FROM sales_records 
        WHERE sale_date >= date('now','-6 months')
        GROUP BY month ORDER BY month
    """).fetchall()
    stats['monthly_sales'] = [dict(row) for row in monthly]
    
    return jsonify(stats)


# ─── API: Labels ────────────────────────────────────────────────
@app.route('/api/labels/generate', methods=['POST'])
def api_labels_generate():
    data = request.json
    product_ids = data.get('product_ids', [])
    
    db = get_db()
    labels = []
    for pid in product_ids:
        product = db.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        if product:
            qr_data = generate_qrcode_base64(product['internal_code'])
            labels.append({
                'id': product['id'],
                'internal_code': product['internal_code'],
                'product_name': (product['product_name_ja'] or product['product_name'])[:50],
                'selling_price': product['selling_price'],
                'qr_code': qr_data
            })
    
    return jsonify({'labels': labels})


@app.route('/api/labels/print', methods=['POST'])
def api_labels_print():
    data = request.json
    labels = data.get('labels', [])
    
    # Generate PDF with labels
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4  # 595.27 x 841.89 points
    
    label_w = 180
    label_h = 120
    margin = 20
    cols = 3
    rows = 6
    
    for i, label in enumerate(labels):
        col = i % cols
        row = i // cols
        if row >= rows:
            c.showPage()
            row = 0
        
        x = margin + col * (label_w + 10)
        y = height - margin - (row + 1) * (label_h + 5)
        
        # Label border
        c.setStrokeColorRGB(0.6, 0.6, 0.6)
        c.setLineWidth(0.5)
        c.rect(x, y, label_w, label_h)
        
        # QR code placeholder
        qr_data = label.get('qr_code', '')
        if qr_data:
            import base64
            from io import BytesIO
            from reportlab.lib.utils import ImageReader
            qr_img = ImageReader(BytesIO(base64.b64decode(qr_data)))
            c.drawImage(qr_img, x + 10, y + label_h - 90, 80, 80)
        
        # Code text
        c.setFont("Helvetica-Bold", 12)
        c.drawString(x + 95, y + label_h - 25, label.get('internal_code', '')[:16])
        
        # Product name
        c.setFont("Helvetica", 8)
        name = label.get('product_name', '')
        if len(name) > 30:
            name = name[:30] + '...'
        c.drawString(x + 5, y + 20, name)
        
        # Price
        price = label.get('selling_price', 0)
        c.setFont("Helvetica-Bold", 14)
        c.drawString(x + 5, y + 5, f'¥{int(price):,}')
    
    c.save()
    buf.seek(0)
    return send_file(buf, mimetype='application/pdf', as_attachment=True, download_name='labels.pdf')


# ─── API: Mercari ───────────────────────────────────────────────
MERCARI_CONDITIONS = {
    '新品・未使用': '新品、未使用',
    '未使用に近い': '未使用に近い',
    '目立った傷や汚れなし': '目立った傷や汚れなし',
    'やや傷や汚れあり': 'やや傷や汚れあり',
    '傷や汚れあり': '傷や汚れあり',
    '全体的に状態が悪い': '全体的に状態が悪い'
}

def generate_mercari_title(product_name_ja, brand=''):
    parts = []
    if brand:
        parts.append(brand)
    # Truncate to a good title
    title = product_name_ja or ''
    if len(title) > 50:
        title = title[:50]
    parts.append(title)
    return ' '.join(parts)


def generate_mercari_description(product, condition='未使用に近い'):
    name = product.get('product_name_ja') or product.get('product_name', '')
    category = product.get('category', 'その他')
    dimensions = product.get('dimensions', '')
    weight = product.get('weight', 0)
    
    desc = f"""【商品説明】
{name}

【商品カテゴリー】
{category}

【商品の状態】
{condition}
※こちらは海外からの輸入品（クロスボーダーリターン商品）です。
※外装パッケージを外した状態での出品となります。
※商品の状態は実物写真にてご確認ください。

【サイズ・重量】
サイズ: {dimensions} cm
重量: 約{weight}kg

【発送方法】
らくらくメルカリ便（匿名配送）
※梱包は簡易包装となります。ご了承ください。

【注意事項】
※海外製品のため、日本国内の正規品とは仕様が異なる場合がございます。
※神経質な方はご購入をお控えください。
※ノークレーム・ノーリターンでお願いいたします。

#メルカリ #輸入雑貨 #お得 #アウトレット"""
    return desc


@app.route('/api/mercari/generate/<int:product_id>', methods=['POST'])
def api_mercari_generate(product_id):
    data = request.json or {}
    db = get_db()
    product = db.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    if not product:
        return jsonify({'error': '商品が見つかりません'}), 404
    
    condition = data.get('condition', '未使用に近い')
    brand = data.get('brand', '')
    price = data.get('price', int((product['selling_price'] or 1000) * 1.1))
    
    title = generate_mercari_title(product['product_name_ja'] or product['product_name'], brand)
    description = generate_mercari_description(dict(product), condition)
    
    # Calculate shipping
    weight = product['weight'] or 0.5
    if weight <= 1:
        shipping = 'ネコポス（210円）'
        shipping_method = 'らくらくメルカリ便 ネコポス'
    elif weight <= 2:
        shipping = '宅急便コンパクト（450円）'
        shipping_method = 'らくらくメルカリ便 宅急便コンパクト'
    else:
        shipping = '宅急便 60サイズ（750円〜）'
        shipping_method = 'らくらくメルカリ便 宅急便'
    
    listing = {
        'product_id': product_id,
        'title': title,
        'description': description,
        'price': price,
        'shipping_method': shipping_method,
        'shipping_info': shipping,
        'condition': condition
    }
    
    # Save to DB
    existing = db.execute("SELECT id FROM mercari_listings WHERE product_id=? AND status='draft'", (product_id,)).fetchone()
    if existing:
        db.execute("""
            UPDATE mercari_listings SET title=?, description=?, price=?, shipping_method=?, condition=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=?
        """, (title, description, price, shipping_method, condition, existing['id']))
    else:
        db.execute("""
            INSERT INTO mercari_listings (product_id, title, description, price, shipping_method, condition)
            VALUES (?,?,?,?,?,?)
        """, (product_id, title, description, price, shipping_method, condition))
    db.commit()
    
    return jsonify({'success': True, 'listing': listing})


@app.route('/api/mercari/draft/<int:product_id>')
def api_mercari_draft(product_id):
    db = get_db()
    listing = db.execute("SELECT * FROM mercari_listings WHERE product_id=? ORDER BY created_at DESC LIMIT 1", (product_id,)).fetchone()
    if listing:
        return jsonify(dict(listing))
    return jsonify(None)


@app.route('/api/mercari/high-value')
def api_mercari_high_value():
    db = get_db()
    products = db.execute("SELECT * FROM products WHERE is_high_value=1 AND status='in_stock' ORDER BY selling_price DESC").fetchall()
    result = []
    for p in products:
        total_in = db.execute("SELECT COALESCE(SUM(qty),0) FROM inbound_records WHERE product_id=?", (p['id'],)).fetchone()[0]
        total_out = db.execute("SELECT COALESCE(SUM(qty),0) FROM sales_records WHERE product_id=?", (p['id'],)).fetchone()[0]
        d = dict(p)
        d['current_stock'] = total_in - total_out
        d['has_listing'] = bool(db.execute("SELECT id FROM mercari_listings WHERE product_id=?", (p['id'],)).fetchone())
        result.append(d)
    return jsonify(result)


# ─── API: Sales Report (销售报表，按平台统计) ───────────────
@app.route('/api/sales/report')
def api_sales_report():
    """按平台/日期统计销售额"""
    start_date = request.args.get('start_date', '')
    end_date = request.args.get('end_date', '')
    platform = request.args.get('platform', '')
    
    db = get_db()
    where = "1=1"
    params = []
    if start_date:
        where += " AND date(s.sale_date) >= ?"
        params.append(start_date)
    if end_date:
        where += " AND date(s.sale_date) <= ?"
        params.append(end_date)
    if platform:
        where += " AND s.platform = ?"
        params.append(platform)
    
    # 按平台统计
    platform_stats = db.execute(f"""
        SELECT 
            COALESCE(s.platform, '未分類') as platform,
            COUNT(*) as sale_count,
            COALESCE(SUM(s.total_amount),0) as total_revenue,
            COALESCE(SUM(s.profit_amount),0) as total_profit,
            COALESCE(AVG(s.total_amount/s.qty),0) as avg_price
        FROM sales_records s
        WHERE {where}
        GROUP BY s.platform
        ORDER BY total_revenue DESC
    """, params).fetchall()
    
    # 按日期统计（最近30天）
    daily_stats = db.execute(f"""
        SELECT 
            date(s.sale_date) as sale_date,
            COALESCE(s.platform, '未分類') as platform,
            COUNT(*) as sale_count,
            COALESCE(SUM(s.total_amount),0) as total_revenue,
            COALESCE(SUM(s.profit_amount),0) as total_profit
        FROM sales_records s
        WHERE {where}
        GROUP BY date(s.sale_date), s.platform
        ORDER BY sale_date DESC
        LIMIT 90
    """, params).fetchall()
    
    return jsonify({
        'platform_stats': [dict(row) for row in platform_stats],
        'daily_stats': [dict(row) for row in daily_stats]
    })


# ─── API: Search ────────────────────────────────────────────────
@app.route('/api/search')
def api_search():
    q = request.args.get('q', '')
    db = get_db()
    results = db.execute("""
        SELECT * FROM products WHERE 
            internal_code LIKE ? OR product_name LIKE ? OR product_name_ja LIKE ? OR category LIKE ?
        ORDER BY updated_at DESC LIMIT 50
    """, (f'%{q}%', f'%{q}%', f'%{q}%', f'%{q}%')).fetchall()
    return jsonify([dict(row) for row in results])


# ─── API: Photo Inbound (拍照识别入库) ──────────────────────────
@app.route('/api/inbound/photo', methods=['POST'])
def api_inbound_photo():
    """拍照上传，自动检测条码"""
    file = request.files.get('image')
    if not file:
        return jsonify({'error': '画像がありません'}), 400
    
    try:
        img = Image.open(file.stream)
        barcodes = pyzbar_decode(img)
        
        if not barcodes:
            return jsonify({'error': 'バーコードが検出できませんでした。手動入力に切り替えてください。', 'type': 'no_barcode'})
        
        detected_codes = []
        for b in barcodes:
            code = b.data.decode('utf-8', errors='ignore')
            barcode_type = b.type
            # Try to match against supplier items
            db = get_db()
            item = db.execute("SELECT * FROM supplier_items WHERE tracking_no=?", (code,)).fetchone()
            product = db.execute("SELECT * FROM products WHERE internal_code=?", (code,)).fetchone()
            
            detected_codes.append({
                'code': code,
                'type': barcode_type,
                'matched': bool(item),
                'match_type': 'supplier_item' if item else ('product' if product else 'unknown')
            })
        
        return jsonify({
            'success': True,
            'detected_count': len(detected_codes),
            'codes': detected_codes
        })
    except Exception as e:
        return jsonify({'error': f'画像処理エラー: {str(e)}'}), 500


# ─── API: AI Photo Recognition (Gemini) ────────────────────────
@app.route('/api/inbound/ai-recognize', methods=['POST'])
def api_inbound_ai_recognize():
    """AI拍照识别实物（Gemini Vision），匹配进货清单中未入库商品"""
    if not GEMINI_AVAILABLE:
        return jsonify({'error': 'AI recognition not configured', 'type': 'not_configured'}), 503
    
    api_key = os.environ.get('GEMINI_API_KEY', '')
    if not api_key:
        return jsonify({'error': 'Gemini API key not set', 'type': 'no_api_key'}), 503
    
    file = request.files.get('image')
    if not file:
        return jsonify({'error': '画像がありません'}), 400
    
    try:
        # Read image and convert to base64
        img = Image.open(file.stream)
        
        # Get all unmatched supplier items (not yet matched to products)
        db = get_db()
        unmatched = db.execute("""
            SELECT id, product_name, category, weight, dimensions, tracking_no
            FROM supplier_items WHERE matched=0
            ORDER BY id
        """).fetchall()
        
        if not unmatched:
            return jsonify({
                'error': '没有未入库的商品。请先上传进货清单。',
                'type': 'no_unmatched'
            })
        
        # Build product list for Gemini prompt
        product_names = []
        for item in unmatched:
            name = item['product_name'] or ''
            if name and len(name) > 5:
                product_names.append(f"- {name[:80]}")
        
        if not product_names:
            # If all names are too short or empty, use all names
            product_names = [f"- {item['product_name'] or '不明商品'}" for item in unmatched[:50]]
        
        products_text = '\n'.join(product_names)
        
        # Configure Gemini
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel('gemini-2.0-flash')
        
        # Convert image to bytes for Gemini
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=80)
        buf.seek(0)
        image_bytes = buf.getvalue()
        
        # Prompt Gemini
        prompt = f"""你是仓库入库助手。我拍了一张商品的照片。

请先描述这张照片里的商品是什么（用日语简短描述，比如「白いワイヤレスイヤホン」「青いTシャツ」等）。

然后，从下面的进货清单中，找出最可能是这个商品的3个候选项（按相似度排列）：

进货清单：
{products_text}

请用JSON格式回答，不要任何其他文字：
{{
  "description": "商品描述（日语）",
  "candidates": [
    {{"index": 序号(1开始), "name": "商品名", "reason": "匹配理由（简短日语）"}},
    {{"index": 序号, "name": "商品名", "reason": "匹配理由"}},
    {{"index": 序号, "name": "商品名", "reason": "匹配理由"}}
  ]
}}
注意：只输出JSON，不要markdown代码块。"""
        
        # Generate content
        response = model.generate_content([
            {'mime_type': 'image/jpeg', 'data': image_bytes},
            prompt
        ])
        
        raw_text = response.text.strip()
        # Clean up markdown code fences if present
        if raw_text.startswith('```'):
            raw_text = raw_text.split('\n', 1)[1]
            if raw_text.endswith('```'):
                raw_text = raw_text[:-3]
        
        ai_result = json.loads(raw_text)
        
        # Match AI candidates to actual supplier item IDs
        candidates = []
        for c in ai_result.get('candidates', []):
            candidate_name = c.get('name', '')
            # Find matching supplier items by name (fuzzy)
            matched_item = None
            for item in unmatched:
                item_name = item['product_name'] or ''
                if candidate_name in item_name or item_name in candidate_name:
                    matched_item = dict(item)
                    break
            
            # If no exact match, do keyword matching
            if not matched_item and candidate_name:
                keywords = candidate_name.split()
                best_score = 0
                for item in unmatched:
                    item_name = item['product_name'] or ''
                    score = sum(1 for kw in keywords if kw in item_name)
                    if score > best_score:
                        best_score = score
                        matched_item = dict(item)
            
            candidates.append({
                'rank': c.get('index', len(candidates) + 1),
                'name': candidate_name,
                'reason': c.get('reason', ''),
                'matched_item': matched_item
            })
        
        return jsonify({
            'success': True,
            'description': ai_result.get('description', ''),
            'candidates': candidates,
            'total_unmatched': len(unmatched)
        })
        
    except json.JSONDecodeError:
        return jsonify({
            'success': True,
            'description': raw_text if 'raw_text' in dir() else '',
            'candidates': [],
            'fallback': True,
            'message': 'AIレスポンスの解���に失敗しました。手動選択��利用ください。'
        })
    except Exception as e:
        return jsonify({'error': f'AI認識エラー: {str(e)}'}), 500


# ─── API: Manual Inbound (手动输入条码入库) ────────────────────
@app.route('/api/inbound/manual', methods=['POST'])
def api_inbound_manual():
    """手动输入条码入库，不需要扫描"""
    data = request.json
    barcode = data.get('barcode', '').strip()
    unit_cost = data.get('unit_cost', 0)
    selling_price = data.get('selling_price', 0)
    qty = data.get('qty', 1)
    is_high_value = data.get('is_high_value', False)
    location = data.get('location', '')
    product_name = data.get('product_name', '')
    category = data.get('category', '')
    weight = data.get('weight')
    dimensions = data.get('dimensions', '')
    
    if not barcode:
        return jsonify({'error': 'バーコードを入力してください'}), 400
    
    db = get_db()
    
    # First try to match existing supplier item
    item = db.execute("SELECT * FROM supplier_items WHERE tracking_no=?", (barcode,)).fetchone()
    
    if not item:
        # Create a "free" supplier item for manual entry
        cur = db.execute("""
            INSERT INTO supplier_items (import_id, tracking_no, product_name, weight, dimensions, category, expected_qty, matched)
            VALUES (NULL, ?, ?, ?, ?, ?, ?, 0)
        """, (barcode, product_name or f'手動登録: {barcode}', weight, dimensions, category, qty))
        supplier_item_id = cur.lastrowid
        db.commit()
        item = db.execute("SELECT * FROM supplier_items WHERE id=?", (supplier_item_id,)).fetchone()
    
    supplier_item_id = item['id']
    
    # Check if already has a product
    existing = db.execute("SELECT * FROM products WHERE supplier_item_id=?", (supplier_item_id,)).fetchone()
    
    if existing:
        product_id = existing['id']
        if unit_cost and not existing['unit_cost']:
            db.execute("UPDATE products SET unit_cost=?, selling_price=?, is_high_value=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                      (unit_cost, selling_price or 0, 1 if is_high_value else 0, product_id))
    else:
        internal_code = generate_internal_code()
        cur = db.execute("""
            INSERT INTO products (internal_code, supplier_item_id, product_name, product_name_ja, category, weight, dimensions, unit_cost, selling_price, is_high_value, location)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (internal_code, supplier_item_id, item['product_name'] or product_name, item['product_name'] or product_name,
              item['category'] or category, item['weight'] or weight, item['dimensions'] or dimensions,
              unit_cost or 0, selling_price or 0, 1 if is_high_value else 0, location))
        product_id = cur.lastrowid
    
    # Record inbound
    db.execute("INSERT INTO inbound_records (product_id, supplier_item_id, qty, unit_cost) VALUES (?,?,?,?)",
              (product_id, supplier_item_id, qty, unit_cost or 0))
    db.execute("UPDATE supplier_items SET matched=1, matched_date=CURRENT_TIMESTAMP WHERE id=?", (supplier_item_id,))
    db.commit()
    
    product = db.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    qr_data = generate_qrcode_base64(product['internal_code'])
    
    return jsonify({
        'success': True,
        'product': dict(product),
        'qr_code': qr_data,
        'is_new': not bool(existing)
    })


# ─── API: Sales History & Receipts ──────────────────────────────
@app.route('/api/sales/recent')
def api_sales_recent():
    """获取最近销售记录（含小票状态），支持平台和日期筛选"""
    db = get_db()
    
    platform = request.args.get('platform', '')
    date = request.args.get('date', '')
    limit = int(request.args.get('limit', 50))
    
    query = """
        SELECT s.*, p.product_name, p.internal_code,
               (SELECT r.id FROM receipts r WHERE r.sale_id = s.id) as receipt_id,
               (SELECT r.printed_count FROM receipts r WHERE r.sale_id = s.id) as printed_count
        FROM sales_records s 
        LEFT JOIN products p ON s.product_id = p.id
        WHERE 1=1
    """
    params = []
    
    if platform:
        query += " AND s.platform = ?"
        params.append(platform)
    
    if date:
        query += " AND date(s.sale_date) = ?"
        params.append(date)
    
    query += " ORDER BY s.sale_date DESC LIMIT ?"
    params.append(limit)
    
    sales = db.execute(query, params).fetchall()
    return jsonify([dict(row) for row in sales])


@app.route('/api/sales/today')
def api_sales_today():
    """本日销售汇总"""
    db = get_db()
    today = datetime.now().strftime('%Y-%m-%d')
    summary = db.execute("""
        SELECT COUNT(*) as count, 
               COALESCE(SUM(total_amount),0) as revenue,
               COALESCE(SUM(profit_amount),0) as profit
        FROM sales_records WHERE date(sale_date) = ?
    """, (today,)).fetchone()
    return jsonify(dict(summary))


def generate_receipt_html(sale, product):
    """Generate receipt HTML for printing"""
    now = datetime.now()
    receipt_no = f"R-{now.strftime('%Y%m%d')}-{sale['id']:04d}"
    payment_labels = {
        'cash': '現金', 'paypay': 'PayPay', 'card': 'カード',
        'mercari': 'メルカリ', 'other': 'その他'
    }
    payment = payment_labels.get(sale.get('payment_method', 'cash'), '現金')
    
    return f"""<!DOCTYPE html>
<html lang="ja">
<head><meta charset="utf-8"><title>領収書</title>
<style>
    @page {{ size: 80mm auto; margin: 5mm; }}
    body {{ font-family: 'MS Gothic', 'Yu Gothic', sans-serif; font-size: 11px; margin: 0; padding: 0; 
           width: 72mm; color: #000; }}
    .header {{ text-align: center; border-bottom: 1px dashed #999; padding-bottom: 5px; margin-bottom: 5px; }}
    .header h2 {{ font-size: 14px; margin: 0 0 3px 0; }}
    .header .sub {{ font-size: 9px; color: #666; }}
    .row {{ display: flex; justify-content: space-between; padding: 2px 0; }}
    .row .label {{ color: #666; }}
    .divider {{ border-top: 1px dashed #999; margin: 5px 0; }}
    .total {{ font-size: 18px; font-weight: bold; text-align: right; }}
    .footer {{ text-align: center; font-size: 9px; color: #666; margin-top: 8px; border-top: 1px dashed #999; padding-top: 5px; }}
    @media print {{ body {{ -webkit-print-color-adjust: exact; }} }}
</style></head>
<body>
<div class="header">
    <h2>領 収 書</h2>
    <div class="sub">滄海明珠合同会社</div>
    <div class="sub">{now.strftime('%Y年%m月%d日 %H:%M')}</div>
    <div style="font-size:9px;margin-top:2px">No. {receipt_no}</div>
</div>
<div class="row"><span class="label">商品名</span><span>{product.get('product_name', '')}</span></div>
<div class="row"><span class="label">管理コード</span><span>{product.get('internal_code', '')}</span></div>
<div class="row"><span class="label">数量</span><span>{sale['qty']} 点</span></div>
<div class="row"><span class="label">単価</span><span>¥{int(sale['unit_price']):,}</span></div>
<div class="divider"></div>
<div class="total">¥{int(sale['total_amount']):,}</div>
<div class="row" style="margin-top:3px"><span class="label">お支払方法</span><span>{payment}</span></div>
<div class="footer">
    <p>ありがとうございました。</p>
    <p style="font-size:8px">※再発行: 滄海明珠合同会社</p>
</div>
<script>window.onload=function(){{window.print();}}</script>
</body></html>"""


@app.route('/api/sales/<int:sale_id>/receipt', methods=['POST'])
def api_sales_receipt(sale_id):
    """生成/补打小票"""
    db = get_db()
    sale = db.execute("""SELECT s.*, p.product_name, p.internal_code 
                       FROM sales_records s LEFT JOIN products p ON s.product_id = p.id 
                       WHERE s.id=?""", (sale_id,)).fetchone()
    if not sale:
        return jsonify({'error': '販売記録が見つかりません'}), 404
    
    product = dict(sale)
    receipt_html = generate_receipt_html(dict(sale), product)
    
    # Save or update receipt
    existing = db.execute("SELECT * FROM receipts WHERE sale_id=?", (sale_id,)).fetchone()
    if existing:
        db.execute("UPDATE receipts SET printed_count=printed_count+1, last_printed=CURRENT_TIMESTAMP, receipt_html=? WHERE sale_id=?",
                  (receipt_html, sale_id))
    else:
        now = datetime.now()
        receipt_no = f"R-{now.strftime('%Y%m%d')}-{sale_id:04d}"
        db.execute("INSERT INTO receipts (sale_id, receipt_no, receipt_html, printed_count, last_printed) VALUES (?,?,?,1,CURRENT_TIMESTAMP)",
                  (sale_id, receipt_no, receipt_html))
    db.commit()
    
    return jsonify({
        'success': True,
        'receipt_html': receipt_html,
        'sale_id': sale_id
    })


# ─── API: Scrap (报废) ──────────────────────────────
@app.route('/api/products/<int:product_id>/scrap', methods=['POST'])
def api_scrap_product(product_id):
    """标记商品为报废（仿品/损坏）"""
    db = get_db()
    data = request.get_json(force=True, silent=True) or {}
    reason = data.get('reason', '')
    note = data.get('note', '')
    
    product = db.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    if not product:
        return jsonify({'error': '商品未找到'}), 404
    
    db.execute("""
        UPDATE products 
        SET status='scrapped', scrap_reason=?, scrapped_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
        WHERE id=?
    """, (reason, product_id))
    db.commit()
    
    return jsonify({'success': True, 'message': f'已标记为报废: {reason}'})


@app.route('/api/scrap/report')
def api_scrap_report():
    """获取报废商品报告"""
    db = get_db()
    start_date = request.args.get('start_date', '')
    end_date = request.args.get('end_date', '')
    
    query = """
        SELECT p.*, 
               si.tracking_no, si.location_code,
               ir.inbound_date
        FROM products p
        LEFT JOIN supplier_items si ON p.supplier_item_id = si.id
        LEFT JOIN inbound_records ir ON p.id = ir.product_id
        WHERE p.status = 'scrapped'
    """
    params = []
    if start_date:
        query += " AND date(p.scrapped_at) >= ?"
        params.append(start_date)
    if end_date:
        query += " AND date(p.scrapped_at) <= ?"
        params.append(end_date)
    query += " ORDER BY p.scrapped_at DESC"
    
    rows = db.execute(query, params).fetchall()
    
    # Summary by reason
    summary = db.execute("""
        SELECT scrap_reason, COUNT(*) as cnt
        FROM products
        WHERE status = 'scrapped'
        GROUP BY scrap_reason
    """).fetchall()
    
    return jsonify({
        'items': [dict(r) for r in rows],
        'summary': {r['scrap_reason']: r['cnt'] for r in summary}
    })


# ─── Page: Scrap Report ─────────────────────────────
@app.route('/scrap')
def page_scrap():
    """报废商品报告页面"""
    return render_template('scrap.html')


# ─── API: All Products for Selection ────────────────────────────
@app.route('/api/products/all')
def api_products_all():
    """获取所有在库商品列表"""
    db = get_db()
    products = db.execute("""
        SELECT p.*, 
            (SELECT COALESCE(SUM(ir.qty),0) FROM inbound_records ir WHERE ir.product_id=p.id) as total_in,
            (SELECT COALESCE(SUM(sr.qty),0) FROM sales_records sr WHERE sr.product_id=p.id) as total_out
        FROM products p WHERE p.status='in_stock'
        ORDER BY p.updated_at DESC
    """).fetchall()
    result = []
    for p in products:
        d = dict(p)
        d['current_stock'] = d['total_in'] - d['total_out']
        result.append(d)
    return jsonify(result)


# ─── Main ───────────────────────────────────────────────────────
if __name__ == '__main__':
    init_db()
    print("\n========================================")
    print("  托盘杂货进销存管理系统")
    print("  http://localhost:5000")
    print("========================================\n")
    app.run(debug=True, host='0.0.0.0', port=5000)
