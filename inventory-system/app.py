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
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))

def jst_today():
    """Return today's date string in JST (YYYY-MM-DD)."""
    return datetime.now(JST).strftime('%Y-%m-%d')

def jst_now():
    """Return current datetime in JST."""
    return datetime.now(JST)
import re
from pathlib import Path

from flask import Flask, request, jsonify, render_template, send_file, g, send_from_directory, make_response
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

# ─── Database: Auto-detect PostgreSQL (Railway) or SQLite (local) ───
DATABASE_URL = os.environ.get('DATABASE_URL', '')
USE_POSTGRES = False
PG_URL = ''
DB_PATH = None

if DATABASE_URL:
    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor
        # Railway provides postgres://user:pass@host:port/dbname
        # psycopg2 needs postgresql:// scheme
        PG_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
        app.logger.info(f"[DB] Attempting PostgreSQL connection...")
        app.logger.info(f"[DB] DATABASE_URL length: {len(DATABASE_URL)}")
        # Test connection immediately with longer timeout
        test_conn = psycopg2.connect(PG_URL, connect_timeout=15)
        test_conn.close()
        USE_POSTGRES = True
        app.logger.info(f"[DB] ✓ PostgreSQL connection successful!")
    except Exception as e:
        app.logger.error(f"[DB] ✗ PostgreSQL failed: {type(e).__name__}: {e}")
        USE_POSTGRES = False
        import sqlite3

BASE_DIR = Path(__file__).parent

if USE_POSTGRES:
    # Try multiple possible upload directory locations for Railway
    # Priority: /data (Railway volume) > /app/data > local uploads
    for candidate in [Path('/data'), Path('/app/data'), BASE_DIR]:
        if candidate.exists():
            UPLOAD_DIR = candidate / 'uploads'
            break
    else:
        # If none exist, use local (will be created)
        UPLOAD_DIR = BASE_DIR / 'uploads'
    app.logger.info(f"[Upload] UPLOAD_DIR candidate path: {UPLOAD_DIR}")
else:
    import sqlite3
    RAILWAY_DATA = Path('/app/data')
    DATA_DIR = RAILWAY_DATA if RAILWAY_DATA.exists() else (BASE_DIR / 'data')
    DB_PATH = DATA_DIR / 'inventory.db'
    UPLOAD_DIR = RAILWAY_DATA / 'uploads' if RAILWAY_DATA.exists() else (BASE_DIR / 'uploads')
    DATA_DIR.mkdir(exist_ok=True, parents=True)

UPLOAD_DIR.mkdir(exist_ok=True, parents=True)

app.logger.info(f"DB mode: {'PostgreSQL' if USE_POSTGRES else f'SQLite ({DB_PATH})'}")

# Initialize database tables (runs on both local & Railway/gunicorn)
_db_initialized = False
try:
    init_db()
    _db_initialized = True
    app.logger.info("Database initialized successfully")
except Exception as e:
    app.logger.error(f"Failed to initialize database: {e}")
    # Don't crash the app - tables will be created on first request if needed

# Ensure database is initialized on first request (backup for Railway/gunicorn)
@app.before_request
def ensure_db_initialized():
    global _db_initialized
    if not _db_initialized:
        try:
            init_db()
            _db_initialized = True
            app.logger.info("Database initialized on first request")
        except Exception as e:
            app.logger.error(f"Failed to initialize DB on request: {e}")


# ─── Database Connection ─────────────────────────────────────────
class DictRow(dict):
    """dict that supports BOTH key access AND integer indexing like sqlite3.Row"""
    def __init__(self, d, columns=None):
        super().__init__(d)
        # Store column order for integer indexing
        self._columns = list(d.keys()) if d else (columns or [])
    
    def __getitem__(self, key):
        if isinstance(key, int):
            # Integer index: return by column position
            return self[self._columns[key]]
        return super().__getitem__(key)
    
    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            raise AttributeError(f"'DictRow' has no attribute '{key}'")


class PostgresCursor:
    """Cursor-like wrapper that mimics sqlite3.Cursor interface"""
    def __init__(self, real_cursor):
        self._cur = real_cursor
        self.lastrowid = None
        self._columns = None  # Store column names after execute
    
    def _store_columns(self):
        """Store column names from cursor description"""
        if self._cur.description:
            self._columns = [desc[0] for desc in self._cur.description]
    
    def fetchone(self):
        self._store_columns()
        row = self._cur.fetchone()
        if row is not None:
            if isinstance(row, dict):
                return DictRow(row, columns=self._columns)
            elif hasattr(row, '__getitem__'):
                # DictRow or tuple - convert to DictRow
                return DictRow(dict(row), columns=self._columns)
        return row
    
    def fetchall(self):
        self._store_columns()
        rows = self._cur.fetchall()
        if rows and self._columns:
            return [DictRow(dict(r), columns=self._columns) for r in rows]
        return rows


class PostgresConnection:
    """Wrapper around psycopg2 connection to mimic sqlite3 interface"""
    # Columns that are BOOLEAN in PostgreSQL but INTEGER in SQLite
    BOOL_COLUMNS = {'matched', 'is_high_value'}

    def __init__(self, url):
        self._conn = psycopg2.connect(url, connect_timeout=15)
        self._conn.autocommit = False
        # Use RealDictCursor by default so queries return dict-like rows (supports .attr and [key])
        self._cursor_factory = RealDictCursor

    def _fix_bool_params(self, sql, params):
        """Convert integer 0/1 to FALSE/TRUE for boolean columns in PostgreSQL"""
        if not params or not isinstance(params, (tuple, list)):
            return sql, params
        new_params = list(params)
        # Find patterns like "colname = ?" or "colname=?" and check if col is boolean
        import re
        for i, val in enumerate(new_params):
            if val in (0, 0.0, 1, 1.0):
                # Look for this parameter's column name in the SQL
                pattern = r'\b(\w+)\s*=\s*%s'
                matches = list(re.finditer(pattern, sql))
                if i < len(matches):
                    col_name = matches[i].group(1).lower()
                    if col_name in self.BOOL_COLUMNS:
                        new_params[i] = bool(val)
        return sql, tuple(new_params)

    def execute(self, sql, params=None):
        if params is None:
            params = ()
        # Replace ? placeholders with %s for psycopg2
        if '?' in sql:
            sql = sql.replace('?', '%s')
        # Fix boolean parameters for PostgreSQL (0/1 -> FALSE/TRUE)
        sql, params = self._fix_bool_params(sql, params)
        cur = self._conn.cursor(cursor_factory=self._cursor_factory)
        # Check if this is an INSERT - we need to get back the lastrowid
        is_insert = sql.strip().upper().startswith('INSERT')
        if is_insert:
            # Add RETURNING id to get the inserted ID
            returning_sql = sql.rstrip().rstrip(';')
            if 'RETURNING' not in returning_sql.upper():
                returning_sql += ' RETURNING id'
            try:
                cur.execute(returning_sql, params)
                result = cur.fetchone()
                if result:
                    wrapper = PostgresCursor(cur)
                    wrapper.lastrowid = result.get('id') if isinstance(result, (dict, DictRow)) else result[0] if hasattr(result, '__getitem__') else None
                    return wrapper
            except Exception as e:
                # If RETURNING fails (e.g., already has it), try normal execute
                self._conn.rollback()
        try:
            cur.execute(sql, params)
        except Exception as e:
            self._conn.rollback()
            raise e
        return PostgresCursor(cur)

    def executemany(self, sql, params_list):
        if '?' in sql and '%s' not in sql:
            sql = sql.replace('?', '%s')
        # Fix boolean parameters for each row
        fixed_params = [self._fix_bool_params(sql, params)[1] for params in params_list]
        cur = self._conn.cursor(cursor_factory=self._cursor_factory)
        cur.executemany(sql, fixed_params)
        return cur

    def executescript(self, sql):
        """Execute multiple SQL statements separated by semicolons"""
        # Split script into individual statements
        statements = [s.strip() for s in sql.split(';') if s.strip()]
        cur = self._conn.cursor(cursor_factory=self._cursor_factory)
        for stmt in statements:
            if stmt:
                try:
                    cur.execute(stmt)
                except Exception as e:
                    # Ignore "already exists" errors for CREATE TABLE IF NOT EXISTS
                    if 'already exists' not in str(e).lower():
                        raise
        return cur

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()

    @property
    def row_factory(self):
        return RealDictCursor  # Returns dict-like rows


class SqliteConnection:
    """Thin wrapper around sqlite3 for consistent interface"""
    def __init__(self, db_path):
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")

    def execute(self, sql, params=None):
        if params is None:
            params = ()
        return self._conn.execute(sql, params)

    def executescript(self, sql):
        return self._conn.executescript(sql)

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


def get_db():
    if 'db' not in g:
        if USE_POSTGRES:
            try:
                g.db = PostgresConnection(PG_URL)
            except Exception as e:
                app.logger.error(f"PostgreSQL connection failed: {e}, falling back to SQLite")
                g.db = SqliteConnection(DB_PATH) if DB_PATH else None
        else:
            g.db = SqliteConnection(DB_PATH) if DB_PATH else None
    return g.db


@app.teardown_appcontext
def close_db(e):
    db = g.pop('db', None)
    if db:
        # PostgreSQL autocommit=False, 需要在关闭前提交以免回滚
        if hasattr(db, 'commit'):
            try:
                db.commit()
            except Exception:
                pass
        db.close()


@app.errorhandler(500)
def handle_500(e):
    app.logger.error(f"500 Error: {e}")
    return jsonify({'error': f'Internal Server Error: {str(e)}'}), 500


@app.errorhandler(Exception)
def handle_exception(e):
    app.logger.error(f"Unhandled exception: {type(e).__name__}: {e}", exc_info=True)
    return jsonify({'error': f'Error: {type(e).__name__}: {str(e)}'}), 500


def init_db():
    """Initialize database tables. Works with both PostgreSQL and SQLite."""
    if USE_POSTGRES:
        _init_postgres()
    else:
        _init_sqlite()


def _get_pk_keyword():
    return "SERIAL PRIMARY KEY" if USE_POSTGRES else "INTEGER PRIMARY KEY AUTOINCREMENT"


def _get_bool_default(val):
    if USE_POSTGRES:
        return "TRUE" if val else "FALSE"
    return str(int(val))


def _init_sqlite():
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
            notes TEXT,
            photo TEXT,
            photo_data TEXT
        );
        CREATE TABLE IF NOT EXISTS sales_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER REFERENCES products(id),
            qty INTEGER DEFAULT 1,
            unit_price REAL,
            total_amount REAL,
            cost_amount REAL,
            profit_amount REAL,
            shipping_fee REAL DEFAULT 0,
            platform_fee REAL DEFAULT 0,
            other_fee REAL DEFAULT 0,
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
            posted_at TIMESTAMP,
            brand TEXT
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
    
    # Migration: add optional columns silently
    for sql in [
        "ALTER TABLE products ADD COLUMN scrap_reason TEXT",
        "ALTER TABLE products ADD COLUMN scrapped_at TIMESTAMP",
        "ALTER TABLE products ADD COLUMN sales_channel TEXT DEFAULT '线下'",
        "ALTER TABLE inbound_records ADD COLUMN photo TEXT",
        "ALTER TABLE mercari_listings ADD COLUMN brand TEXT",
        "ALTER TABLE sales_records ADD COLUMN shipping_fee REAL DEFAULT 0",
        "ALTER TABLE sales_records ADD COLUMN platform_fee REAL DEFAULT 0",
        "ALTER TABLE sales_records ADD COLUMN other_fee REAL DEFAULT 0",
        "ALTER TABLE supplier_items ADD COLUMN inbound_qty INTEGER DEFAULT 0",
    ]:
        try: db.execute(sql)
        except: pass

    # 数据迁移: 补填旧数据的 inbound_qty（根据 inbound_records 实际数量）
    try:
        db.execute("""UPDATE supplier_items SET inbound_qty = COALESCE(
            (SELECT SUM(qty) FROM inbound_records WHERE supplier_item_id = supplier_items.id), 0
        ) WHERE COALESCE(inbound_qty, 0) = 0""")
        count = db.execute("SELECT COUNT(*) FROM supplier_items WHERE inbound_qty > 0").fetchone()[0]
        if count > 0:
            app.logger.info(f"[Migration] Fixed inbound_qty for {count} supplier_items")
    except Exception as e:
        app.logger.warning(f"[Migration] inbound_qty fix failed (non-critical): {e}")

    db.commit()
    db.close()


def _init_postgres():
    conn = psycopg2.connect(PG_URL, connect_timeout=15)
    cur = conn.cursor()
    pk = _get_pk_keyword()
    app.logger.info(f"[PostgreSQL] Creating tables with pk={pk}...")

    statements = [
        f"""CREATE TABLE IF NOT EXISTS supplier_imports (
            id {pk}, filename TEXT, import_date TIMESTAMP DEFAULT NOW(),
            total_items INTEGER DEFAULT 0
        )""",
        f"""CREATE TABLE IF NOT EXISTS supplier_items (
            id {pk}, import_id INTEGER REFERENCES supplier_imports(id) ON DELETE SET NULL,
            tracking_no TEXT, location_code TEXT, weight REAL, dimensions TEXT,
            product_name TEXT, expected_qty INTEGER DEFAULT 1, category TEXT,
            unit_cost REAL, matched BOOLEAN DEFAULT FALSE, matched_date TIMESTAMP,
            inbound_qty INTEGER DEFAULT 0
        )""",
        f"""CREATE TABLE IF NOT EXISTS products (
            id {pk}, internal_code TEXT UNIQUE NOT NULL,
            supplier_item_id INTEGER REFERENCES supplier_items(id) ON DELETE SET NULL,
            product_name TEXT, product_name_ja TEXT, category TEXT, weight REAL,
            dimensions TEXT, unit_cost REAL, selling_price REAL,
            is_high_value INTEGER DEFAULT 0, high_value_reason TEXT,
            status TEXT DEFAULT 'in_stock', scrap_reason TEXT, scrapped_at TIMESTAMP,
            location TEXT, created_at TIMESTAMP DEFAULT NOW(), updated_at TIMESTAMP DEFAULT NOW()
        )""",
        f"""CREATE TABLE IF NOT EXISTS inbound_records (
            id {pk}, product_id INTEGER REFERENCES products(id) ON DELETE CASCADE,
            supplier_item_id INTEGER REFERENCES supplier_items(id) ON DELETE SET NULL,
            qty INTEGER DEFAULT 1, unit_cost REAL,
            inbound_date TIMESTAMP DEFAULT NOW(), notes TEXT, photo TEXT,
            photo_data TEXT
        )""",
        f"""CREATE TABLE IF NOT EXISTS sales_records (
            id {pk}, product_id INTEGER REFERENCES products(id) ON DELETE CASCADE,
            qty INTEGER DEFAULT 1, unit_price REAL, total_amount REAL,
            cost_amount REAL, profit_amount REAL,
            shipping_fee REAL DEFAULT 0, platform_fee REAL DEFAULT 0, other_fee REAL DEFAULT 0,
            payment_method TEXT DEFAULT 'cash', platform TEXT,
            sale_date TIMESTAMP DEFAULT NOW(), notes TEXT
        )""",
        f"""CREATE TABLE IF NOT EXISTS mercari_listings (
            id {pk}, product_id INTEGER REFERENCES products(id) ON DELETE CASCADE,
            title TEXT, description TEXT, price INTEGER, shipping_method TEXT,
            condition TEXT, status TEXT DEFAULT 'draft', listing_url TEXT,
            created_at TIMESTAMP DEFAULT NOW(), posted_at TIMESTAMP, brand TEXT
        )""",
        f"""CREATE TABLE IF NOT EXISTS receipts (
            id {pk}, sale_id INTEGER REFERENCES sales_records(id) ON DELETE CASCADE,
            receipt_no TEXT UNIQUE, receipt_html TEXT,
            printed_count INTEGER DEFAULT 0, last_printed TIMESTAMP,
            created_at TIMESTAMP DEFAULT NOW()
        )""",
        f"""CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)""",
    ]

    for stmt in statements:
        try: cur.execute(stmt)
        except Exception as e:
            app.logger.warning(f"Init table warning: {e}")

    # Create indexes
    for idx_sql in [
        "CREATE INDEX IF NOT EXISTS idx_supplier_items_tracking ON supplier_items(tracking_no)",
        "CREATE INDEX IF NOT EXISTS idx_products_internal ON products(internal_code)",
        "CREATE INDEX IF NOT EXISTS idx_supplier_items_import ON supplier_items(import_id)",
    ]:
        try: cur.execute(idx_sql)
        except: pass

    # Add optional columns (PostgreSQL style)
    for col_sql in [
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS scrap_reason TEXT",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS scrapped_at TIMESTAMP",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS sales_channel TEXT DEFAULT '线下'",
        "ALTER TABLE inbound_records ADD COLUMN IF NOT EXISTS photo TEXT",
        "ALTER TABLE inbound_records ADD COLUMN IF NOT EXISTS photo_data TEXT",
        "ALTER TABLE mercari_listings ADD COLUMN IF NOT EXISTS brand TEXT",
        "ALTER TABLE sales_records ADD COLUMN IF NOT EXISTS shipping_fee REAL DEFAULT 0",
        "ALTER TABLE sales_records ADD COLUMN IF NOT EXISTS platform_fee REAL DEFAULT 0",
        "ALTER TABLE sales_records ADD COLUMN IF NOT EXISTS other_fee REAL DEFAULT 0",
        "ALTER TABLE supplier_items ADD COLUMN IF NOT EXISTS inbound_qty INTEGER DEFAULT 0",
    ]:
        try: cur.execute(col_sql)
        except: pass

    # 数据迁移 (PG): 补填旧数据的 inbound_qty
    try:
        cur.execute("""UPDATE supplier_items SET inbound_qty = COALESCE(
            (SELECT SUM(qty) FROM inbound_records WHERE supplier_item_id = supplier_items.id), 0
        ) WHERE COALESCE(inbound_qty, 0) = 0""")
        conn.commit()
    except Exception as e:
        app.logger.warning(f"[Migration] PG inbound_qty fix failed (non-critical): {e}")

    conn.commit()
    cur.close()
    conn.close()


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
    total_shipping_fee = db.execute("SELECT COALESCE(SUM(shipping_fee),0) FROM sales_records").fetchone()[0]
    total_platform_fee = db.execute("SELECT COALESCE(SUM(platform_fee),0) FROM sales_records").fetchone()[0]
    total_other_fee = db.execute("SELECT COALESCE(SUM(other_fee),0) FROM sales_records").fetchone()[0]
    total_fees = total_shipping_fee + total_platform_fee + total_other_fee
    total_cost = db.execute("SELECT COALESCE(SUM(cost_amount),0) FROM sales_records").fetchone()[0]
    net_profit = total_revenue - total_cost - total_fees
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
        low_stock=stock_data,
        total_shipping_fee=total_shipping_fee,
        total_platform_fee=total_platform_fee,
        total_other_fee=total_other_fee,
        total_fees=total_fees,
        total_cost=total_cost,
        net_profit=net_profit)


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
    from time import time
    try:
        db = get_db()
        if not db:
            return render_template('inventory.html', ssr_data=[], ssr_categories=[], now_ts=int(time()))
        
        # Pre-fetch data server-side for SSR (table rendered by Jinja2, not JS)
        try:
            # 关联查询照片和跟踪号
            products = db.execute("""SELECT p.*,
                (SELECT photo FROM inbound_records WHERE product_id=p.id AND photo IS NOT NULL AND photo != '' ORDER BY id DESC LIMIT 1) as photo,
                (SELECT photo_data FROM inbound_records WHERE product_id=p.id AND photo_data IS NOT NULL AND photo_data != '' ORDER BY id DESC LIMIT 1) as photo_data,
                (SELECT si.tracking_no FROM supplier_items si WHERE si.id=p.supplier_item_id LIMIT 1) as tracking_no
                FROM products p ORDER BY p.updated_at DESC""").fetchall()
        except Exception as e:
            app.logger.error(f"Inventory query failed: {e}")
            products = []
        
        inventory_data = []
        for p in products:
            try:
                p_dict = dict(p)
                total_in = db.execute("SELECT COALESCE(SUM(qty),0) FROM inbound_records WHERE product_id=?", (p_dict.get('id'),)).fetchone()
                total_in = total_in[0] if total_in else 0
                total_out = db.execute("SELECT COALESCE(SUM(qty),0) FROM sales_records WHERE product_id=?", (p_dict.get('id'),)).fetchone()
                total_out = total_out[0] if total_out else 0
                total_revenue = db.execute("SELECT COALESCE(SUM(total_amount),0) FROM sales_records WHERE product_id=?", (p_dict.get('id'),)).fetchone()
                total_revenue = total_revenue[0] if total_revenue else 0
                total_fees = db.execute("SELECT COALESCE(SUM(shipping_fee+platform_fee+other_fee),0) FROM sales_records WHERE product_id=?", (p_dict.get('id'),)).fetchone()
                total_fees = total_fees[0] if total_fees else 0
                p_dict['current_stock'] = total_in - total_out
                p_dict['total_in'] = total_in
                p_dict['total_out'] = total_out
                p_dict['total_revenue'] = total_revenue
                p_dict['total_fees'] = total_fees
                p_dict['net_profit'] = total_revenue - (p_dict.get('unit_cost') or 0) * total_out - total_fees
                # 添加照片URL（优先DB base64 → 磁盘文件兜底）
                if p_dict.get('photo_data'):
                    p_dict['photo_url'] = f"data:image/jpeg;base64,{p_dict['photo_data']}"
                elif p_dict.get('photo'):
                    p_dict['photo_url'] = f'/uploads/{p_dict["photo"]}'
                else:
                    p_dict['photo_url'] = None
                inventory_data.append(p_dict)
            except Exception as e:
                app.logger.error(f"Error processing product {p}: {e}")
                continue

        # Unique categories for filter dropdown
        categories = sorted(set(d.get('category') or '' for d in inventory_data))

        response = make_response(render_template(
            'inventory.html',
            ssr_data=inventory_data,
            ssr_categories=categories,
            now_ts=int(time())
        ))
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        return response
    except Exception as e:
        app.logger.error(f"Inventory page error: {e}", exc_info=True)
        raise


@app.route('/inventory-simple')
def inventory_simple_page():
    """Simple server-side rendered inventory - no JS needed"""
    db = get_db()
    # 关联查询照片（取最近一次入库的照片）
    products = db.execute("""SELECT p.*, 
        (SELECT photo FROM inbound_records WHERE product_id=p.id ORDER BY id DESC LIMIT 1) as photo 
        FROM products p ORDER BY p.updated_at DESC""").fetchall()

    html = '''<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>在庫一覧 (簡易版)</title>
<style>
body{font-family:sans-serif;padding:16px;max-width:1200px;margin:0 auto;background:#f8f9fa}
h2{color:#1a1a2e}.ok{color:green}.card{background:#fff;border-radius:12px;padding:16px;margin-bottom:16px;box-shadow:0 1px 3px rgba(0,0,0,.1)}
table{width:100%;border-collapse:collapse;margin-top:12px}
th,td{border:1px solid #e5e7eb;padding:8px 12px;text-align:left}
th{background:#f9fafb;font-weight:600}.text-end{text-align:right}.text-center{text-align:center}
.badge{padding:2px 8px;border-radius:6px;font-size:.8em;color:#fff}
.bg-success{background:#0d904f}.bg-warning{background:#f5a623}.bg-danger{background:#d93025}.bg-info{background:#1a73e8}
code{background:#f0f0f0;padding:1px 4px;border-radius:3px;font-size:.85em}
.nav{margin-bottom:16px}.nav a{color:#1a73e8;margin-right:12px;text-decoration:none}
.nav a:hover{text-decoration:underline}
</style>
</head>
<body>
<div class="nav">
<a href="/">ダッシュボード</a>
<a href="/inventory"><strong>在庫一覧</strong></a>
<a href="/inventory-simple">在庫一覧(簡易版)</a>
<a href="/debug-data">データ診断</a>
</div>
<h2>在庫一覧 (サーバーサイドレンダリング)</h2>
<p>このページはJavaScriptに依存せず、サーバーで直接データを表示します。</p>
'''

    if not products:
        html += '<div class="card"><p>まだ商品が登録されていません。</p>'
        html += '<p><a href="/debug-data">データ状態を確認</a></p></div>'
    else:
        html += '<div class="card"><table><thead><tr>'
        html += '<th>内部コード</th><th>商品名</th><th>カテゴリ</th><th>仕入単価</th><th>販売価格</th>'
        html += '<th>入庫数</th><th>販売数</th><th>在庫数</th><th>売上</th><th>状態</th>'
        html += '</tr></thead><tbody>'

        for p in products:
            total_in = db.execute("SELECT COALESCE(SUM(qty),0) FROM inbound_records WHERE product_id=?", (p['id'],)).fetchone()[0]
            total_out = db.execute("SELECT COALESCE(SUM(qty),0) FROM sales_records WHERE product_id=?", (p['id'],)).fetchone()[0]
            total_revenue = db.execute("SELECT COALESCE(SUM(total_amount),0) FROM sales_records WHERE product_id=?", (p['id'],)).fetchone()[0]
            stock = total_in - total_out

            stock_cls = 'bg-danger' if stock <= 0 else ('bg-warning' if stock <= 3 else 'bg-success')
            status = '売切れ' if p['status'] == 'sold_out' else ('残りわずか' if stock <= 3 else '在庫あり')
            status_cls = 'bg-danger' if p['status'] == 'sold_out' else ('bg-warning' if stock <= 3 else 'bg-success')

            html += '<tr>'
            html += f'<td><code>{p["internal_code"]}</code></td>'
            html += f'<td>{p["product_name"] or ""}</td>'
            html += f'<td>{p["category"] or "-"}</td>'
            html += f'<td class="text-end">¥{int(p["unit_cost"] or 0):,}</td>'
            html += f'<td class="text-end">¥{int(p["selling_price"] or 0):,}</td>'
            html += f'<td class="text-center">{total_in}</td>'
            html += f'<td class="text-center">{total_out}</td>'
            html += f'<td class="text-center"><span class="badge {stock_cls}">{stock}</span></td>'
            html += f'<td class="text-end">¥{int(total_revenue or 0):,}</td>'
            html += f'<td><span class="badge {status_cls}">{status}</span></td>'
            html += '</tr>'

        html += '</tbody></table></div>'

    html += f'<p style="margin-top:20px;color:#666;font-size:.85em">'
    if USE_POSTGRES:
        html += f'DBモード: PostgreSQL (データは永続化されています)'
    else:
        html += f'DBパス: {DB_PATH} | '
        html += f'商品数: {len(products)} | '
        if DB_PATH.exists():
            html += f'DBサイズ: {DB_PATH.stat().st_size} bytes'
    html += '</p></body></html>'

    response = make_response(html)
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return response


@app.route('/labels')
def labels_page():
    from time import time
    try:
        db = get_db()
        if not db:
            return render_template('labels.html', ssr_data=[], now_ts=int(time()))
        
        # SSR: 服务端直接查询数据（与 /inventory 相同逻辑）
        try:
            products = db.execute("""SELECT p.*,
                (SELECT photo FROM inbound_records WHERE product_id=p.id ORDER BY id DESC LIMIT 1) as photo,
                (SELECT si.tracking_no FROM supplier_items si WHERE si.id=p.supplier_item_id LIMIT 1) as tracking_no
                FROM products p ORDER BY p.updated_at DESC""").fetchall()
        except Exception as e:
            app.logger.error(f"Labels query failed: {e}")
            products = []
        
        label_data = []
        for p in products:
            try:
                p_dict = dict(p)
                total_in = db.execute("SELECT COALESCE(SUM(qty),0) FROM inbound_records WHERE product_id=?", (p_dict.get('id'),)).fetchone()
                total_in = total_in[0] if total_in else 0
                total_out = db.execute("SELECT COALESCE(SUM(qty),0) FROM sales_records WHERE product_id=?", (p_dict.get('id'),)).fetchone()
                total_out = total_out[0] if total_out else 0
                p_dict['current_stock'] = total_in - total_out
                p_dict['total_in'] = total_in
                p_dict['total_out'] = total_out
                if p_dict.get('photo'):
                    p_dict['photo_url'] = f'/uploads/{p_dict["photo"]}'
                else:
                    p_dict['photo_url'] = None
                label_data.append(p_dict)
            except Exception as e:
                app.logger.error(f"Error processing label product {p}: {e}")
                continue

        # Unique categories for filter dropdown
        categories = sorted(set(d.get('category') or '' for d in label_data))

        response = make_response(render_template(
            'labels.html',
            ssr_data=label_data,
            ssr_categories=categories,
            now_ts=int(time())
        ))
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        return response
    except Exception as e:
        app.logger.error(f"Labels page error: {e}", exc_info=True)
        raise


@app.route('/mercari')
def mercari_page():
    return render_template('mercari.html')


@app.route('/guide')
def guide_page():
    return render_template('guide.html')

@app.route('/debug-data')
def debug_data():
    """调试页面：显示数据库状态"""
    db = get_db()
    tables = ['supplier_imports', 'supplier_items', 'products', 'inbound_records', 'sales_records', 'mercari_listings']
    result = []
    for table in tables:
        try:
            count = db.execute(f"SELECT COUNT(*) as cnt FROM {table}").fetchone()['cnt']
            result.append({'table': table, 'count': count})
        except:
            result.append({'table': table, 'count': 'ERROR'})
    
    db_info = {
        'mode': 'PostgreSQL' if USE_POSTGRES else 'SQLite',
        'path': PG_URL if USE_POSTGRES else str(DB_PATH),
        'tables': result
    }
    
    has_data = any(isinstance(t['count'], int) and t['count'] > 0 for t in result)
    
    html = '<html><head><meta charset="utf-8"><title>データベース診断</title>'
    html += '<style>body{font-family:sans-serif;padding:20px;max-width:600px;margin:0 auto}'
    html += '.ok{color:green}.warn{color:orange}.error{color:red}'
    html += 'table{width:100%;border-collapse:collapse;margin:10px 0}'
    html += 'td,th{border:1px solid #ddd;padding:8px;text-align:left}'
    html += 'th{background:#f5f5f5}</style></head><body>'
    html += '<h2>データベース診断</h2>'
    html += f'<p>DBモード: <strong>{db_info["mode"]}</strong></p>'
    
    html += '<table><tr><th>テーブル</th><th>レコード数</th></tr>'
    for t in result:
        cls = 'ok' if isinstance(t['count'], int) and t['count'] > 0 else 'warn'
        html += f'<tr><td>{t["table"]}</td><td class="{cls}">{t["count"]}</td></tr>'
    html += '</table>'
    
    if not has_data:
        html += '<p class="warn"><strong>まだデータがありません。以下の手順で始めてください：</strong></p>'
        html += '<ol><li><a href="/import">仕入リスト管理</a> でExcelファイルをアップロード</li>'
        html += '<li><a href="/inbound">入庫管理</a> で商品を選択し入庫</li>'
        html += '<li><a href="/inventory">在庫一覧</a> で確認</li></ol>'
    
    html += '<p style="margin-top:20px"><a href="/inventory">在庫一覧に戻る</a></p></body></html>'
    return html


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
    # First check internal products by internal_code (QR code scan)
    product = db.execute("SELECT * FROM products WHERE internal_code=? AND status != 'scrapped'", (barcode,)).fetchone()
    if product:
        return _format_product_result(db, product)
    
    # Check supplier items by tracking_no (original barcode)
    item = db.execute("SELECT * FROM supplier_items WHERE tracking_no=?", (barcode,)).fetchone()
    if item:
        # If this supplier_item already linked to a product, return the product
        linked = db.execute("SELECT * FROM products WHERE supplier_item_id=? AND status != 'scrapped'", (item['id'],)).fetchone()
        if linked:
            return _format_product_result(db, linked)
        # Otherwise return supplier_item (for inbound matching)
        result = dict(item)
        result['type'] = 'supplier_item'
        return jsonify(result)
    
    return jsonify({'error': '見つかりません', 'type': 'unknown'})


def _format_product_result(db, product):
    result = dict(product)
    total_in = db.execute("SELECT COALESCE(SUM(qty),0) FROM inbound_records WHERE product_id=?", (product['id'],)).fetchone()[0]
    total_out = db.execute("SELECT COALESCE(SUM(qty),0) FROM sales_records WHERE product_id=?", (product['id'],)).fetchone()[0]
    result['current_stock'] = total_in - total_out
    result['type'] = 'product'
    return jsonify(result)


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
    photo_base64 = data.get('photo_base64', '')  # 新增：商品照片
    
    db = get_db()
    item = db.execute("SELECT * FROM supplier_items WHERE id=?", (supplier_item_id,)).fetchone()
    if not item:
        return jsonify({'error': '商品が見つかりません'}), 404

    # 部分入库支持：检查是否强制创建新商品（同一跟踪号不同型号）
    force_new = data.get('force_new', False)
    
    # Check if already has a product
    existing = db.execute("SELECT * FROM products WHERE supplier_item_id=?", (supplier_item_id,)).fetchone()
    
    # 如果 force_new=True，忽略 existing，创建新商品
    if force_new:
        existing = None
    
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
    
    # Save photo if provided
    photo_filename = None
    photo_encoded = None  # base64 data for DB storage
    if photo_base64 and photo_base64.startswith('data:image/'):
        try:
            # Extract base64 data after comma
            header, encoded = photo_base64.split(',', 1)
            ext = header.split('/')[1].split(';')[0]  # e.g., 'jpeg' or 'png'
            if ext == 'jpeg':
                ext = 'jpg'
            photo_filename = f"inbound_{product_id}_{uuid.uuid4().hex[:8]}.{ext}"
            photo_path = UPLOAD_DIR / photo_filename
            photo_encoded = encoded  # 保存base64到DB

            # 确保目录存在且可写（disk backup，非必须）
            UPLOAD_DIR.mkdir(exist_ok=True, parents=True)
            try:
                with open(photo_path, 'wb') as f:
                    f.write(base64.b64decode(encoded))
                if not photo_path.exists() or photo_path.stat().st_size == 0:
                    app.logger.warning(f"[Photo] Disk save failed (non-critical): {photo_path}")
            except Exception as e:
                app.logger.warning(f"[Photo] Disk save error (non-critical): {e}")

        except Exception as e:
            app.logger.error(f"[Photo] ERROR processing photo: {e}")
            photo_filename = None
            photo_encoded = None
    
    # Record inbound (DB保存photo_data — 永続化)
    if inbound_date:
        db.execute("INSERT INTO inbound_records (product_id, supplier_item_id, qty, unit_cost, inbound_date, photo, photo_data) VALUES (?,?,?,?,?,?,?)",
                  (product_id, supplier_item_id, qty, unit_cost or 0, inbound_date, photo_filename, photo_encoded))
    else:
        db.execute("INSERT INTO inbound_records (product_id, supplier_item_id, qty, unit_cost, photo, photo_data) VALUES (?,?,?,?,?,?)",
                  (product_id, supplier_item_id, qty, unit_cost or 0, photo_filename, photo_encoded))

    # 更新 inbound_qty（部分入库支持）
    db.execute("UPDATE supplier_items SET inbound_qty = COALESCE(inbound_qty,0) + ? WHERE id=?",
               (qty, supplier_item_id))
    # 只有入库数量 >= 预报数量时才标记为已完成
    db.execute("""UPDATE supplier_items
                   SET matched=TRUE, matched_date=CURRENT_TIMESTAMP
                   WHERE id=? AND inbound_qty >= expected_qty""",
               (supplier_item_id,))
    db.commit()
    
    product = db.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    qr_data = generate_qrcode_base64(product['internal_code'])
    
    result = {
        'success': True,
        'product': dict(product),
        'qr_code': qr_data,
        'is_new': not bool(existing)
    }
    if photo_filename and photo_encoded:
        result['photo_url'] = f"data:image/jpeg;base64,{photo_encoded}"
    elif photo_filename:
        result['photo_url'] = f'/uploads/{photo_filename}'
    
    return jsonify(result)


@app.route('/api/inbound/unmatched')
def api_inbound_unmatched():
    """返回未匹配 + 部分入库中的供应商商品"""
    db = get_db()
    # 显示: 未匹配(matched=FALSE) OR 部分入库中(inbound_qty < expected_qty)
    items = db.execute("""SELECT * FROM supplier_items
                           WHERE matched=FALSE OR (matched=TRUE AND inbound_qty < expected_qty)
                           ORDER BY matched ASC, id ASC""").fetchall()
    result = []
    for row in items:
        d = dict(row)
        d['inbound_qty'] = d.get('inbound_qty') or 0
        d['expected_qty'] = d.get('expected_qty') or 1
        d['progress_pct'] = int((d['inbound_qty'] / d['expected_qty']) * 100) if d['expected_qty'] > 0 else 0
        result.append(d)
    return jsonify(result)


# ─── API: Sales (销售) ──────────────────────────────────────────
@app.route('/api/sales/create', methods=['POST'])
def api_sales_create():
    data = request.json
    product_id = data.get('product_id')
    qty = data.get('qty', 1)
    unit_price = data.get('unit_price', 0)
    payment_method = data.get('payment_method', 'cash')
    platform = data.get('platform', '')
    note = data.get('note', '')
    
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
    # Fees (only for platform sales: mercari, yahoo, rakuten)
    shipping_fee = data.get('shipping_fee', 0) or 0
    platform_fee = data.get('platform_fee', 0) or 0
    other_fee = data.get('other_fee', 0) or 0
    profit_amount = total_amount - cost_amount - shipping_fee - platform_fee - other_fee
    
    cur = db.execute("""
        INSERT INTO sales_records (product_id, qty, unit_price, total_amount, cost_amount, profit_amount, shipping_fee, platform_fee, other_fee, payment_method, platform, notes)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    """, (product_id, qty, unit_price, total_amount, cost_amount, profit_amount,
          shipping_fee, platform_fee, other_fee, payment_method, platform, note))
    
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
        'profit': profit_amount, 'net_profit': profit_amount,
        'shipping_fee': shipping_fee, 'platform_fee': platform_fee, 'other_fee': other_fee,
        'new_stock': new_stock,
        'payment_method': payment_method
    })


# ─── API: Batch Sales Create (购物篮统一结算) ────────────────
@app.route('/api/sales/batch-create', methods=['POST'])
def api_sales_batch_create():
    """购物篮批量销售：一次结算多件商品，费用只计入第一件"""
    data = request.json
    items = data.get('items', [])      # [{product_id, qty, unit_price}]
    payment_method = data.get('payment_method', 'cash')
    platform = data.get('platform', '')
    shipping_fee = data.get('shipping_fee', 0) or 0
    platform_fee = data.get('platform_fee', 0) or 0
    other_fee = data.get('other_fee', 0) or 0
    note = data.get('note', '')
    print_receipt = data.get('print_receipt', False)

    if not items or len(items) == 0:
        return jsonify({'error': '购物篮为空'}), 400

    db = get_db()
    cur = None
    sale_ids = []
    total_qty = 0
    total_amount = 0
    total_profit = 0

    try:
        for idx, item in enumerate(items):
            product_id = item.get('product_id')
            qty = item.get('qty', 1)
            unit_price = item.get('unit_price', 0)

            product = db.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
            if not product:
                raise Exception(f'商品ID {product_id} 不存在')

            # 检查库存
            total_in = db.execute("SELECT COALESCE(SUM(qty),0) FROM inbound_records WHERE product_id=?", (product_id,)).fetchone()[0]
            total_out = db.execute("SELECT COALESCE(SUM(qty),0) FROM sales_records WHERE product_id=?", (product_id,)).fetchone()[0]
            current_stock = total_in - total_out
            if current_stock < qty:
                raise Exception(f'商品 {product["product_name"]} 库存不足（当前: {current_stock}，需: {qty}）')

            item_total = unit_price * qty
            cost_amount = (product['unit_cost'] or 0) * qty

            # 费用分摊：只有第一件商品承担全部费用
            item_shipping = shipping_fee if idx == 0 else 0
            item_platform = platform_fee if idx == 0 else 0
            item_other = other_fee if idx == 0 else 0
            profit_amount = item_total - cost_amount - item_shipping - item_platform - item_other

            cur = db.execute("""
                INSERT INTO sales_records (product_id, qty, unit_price, total_amount, cost_amount, profit_amount,
                                        shipping_fee, platform_fee, other_fee, payment_method, platform, notes)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """, (product_id, qty, unit_price, item_total, cost_amount, profit_amount,
                   item_shipping, item_platform, item_other, payment_method, platform, note))

            sale_id = cur.lastrowid
            sale_ids.append(sale_id)

            # 更新库存状态
            new_stock = current_stock - qty
            if new_stock <= 0:
                db.execute("UPDATE products SET status='sold_out', updated_at=CURRENT_TIMESTAMP WHERE id=?", (product_id,))

            total_qty += qty
            total_amount += item_total
            total_profit += profit_amount

        db.commit()

        return jsonify({
            'success': True,
            'sale_ids': sale_ids,
            'total_qty': total_qty,
            'total_amount': total_amount,
            'total_profit': total_profit,
            'print_receipt': print_receipt
        })
    except Exception as e:
        if USE_POSTGRES:
            db.rollback()
        else:
            pass
        return jsonify({'error': str(e)}), 400


# ─── API: Inventory ─────────────────────────────────────────────
@app.route('/api/inventory/list')
def api_inventory_list():
    db = get_db()
    # 关联查询照片和跟踪号（取最近一次入库的照片）
    products = db.execute("""
        SELECT p.*,
               (SELECT photo FROM inbound_records WHERE product_id=p.id AND photo IS NOT NULL AND photo != '' ORDER BY id DESC LIMIT 1) as photo,
               (SELECT photo_data FROM inbound_records WHERE product_id=p.id AND photo_data IS NOT NULL AND photo_data != '' ORDER BY id DESC LIMIT 1) as photo_data,
               (SELECT si.tracking_no FROM supplier_items si WHERE si.id=p.supplier_item_id LIMIT 1) as tracking_no
        FROM products p
        ORDER BY p.updated_at DESC
    """).fetchall()
    result = []
    for p in products:
        total_in = db.execute("SELECT COALESCE(SUM(qty),0) FROM inbound_records WHERE product_id=?", (p['id'],)).fetchone()[0]
        total_out = db.execute("SELECT COALESCE(SUM(qty),0) FROM sales_records WHERE product_id=?", (p['id'],)).fetchone()[0]
        d = dict(p)
        d['current_stock'] = total_in - total_out
        d['total_in'] = total_in
        d['total_out'] = total_out
        d['total_revenue'] = db.execute("SELECT COALESCE(SUM(total_amount),0) FROM sales_records WHERE product_id=?", (p['id'],)).fetchone()[0]
        d['total_fees'] = db.execute("SELECT COALESCE(SUM(shipping_fee+platform_fee+other_fee),0) FROM sales_records WHERE product_id=?", (p['id'],)).fetchone()[0]
        d['net_profit'] = d['total_revenue'] - (d.get('unit_cost') or 0) * total_out - d['total_fees']
        # 添加照片URL（优先DB base64 → 磁盘文件兜底）
        if d.get('photo_data'):
            d['photo_url'] = f"data:image/jpeg;base64,{d['photo_data']}"
        elif d.get('photo'):
            d['photo_url'] = f'/uploads/{d["photo"]}'
        else:
            d['photo_url'] = None
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
        'high_value_count': db.execute("SELECT COUNT(*) FROM products WHERE is_high_value=TRUE").fetchone()[0],
    }
    
    # By category
    categories = db.execute("""
        SELECT category, COUNT(*) as cnt, COALESCE(SUM(selling_price),0) as total_value
        FROM products GROUP BY category ORDER BY cnt DESC
    """).fetchall()
    stats['categories'] = [dict(row) for row in categories]
    
    # Monthly sales (JST timezone)
    if USE_POSTGRES:
        monthly = db.execute("""
            SELECT TO_CHAR(sale_date + INTERVAL '9 hours', 'YYYY-MM') as month, 
                   COUNT(*) as cnt, COALESCE(SUM(total_amount),0) as revenue,
                   COALESCE(SUM(profit_amount),0) as profit
            FROM sales_records 
            WHERE sale_date >= NOW() - INTERVAL '6 months'
            GROUP BY month ORDER BY month
        """).fetchall()
    else:
        monthly = db.execute("""
            SELECT strftime('%Y-%m', sale_date, '+9 hours') as month, 
                   COUNT(*) as cnt, COALESCE(SUM(total_amount),0) as revenue,
                   COALESCE(SUM(profit_amount),0) as profit
            FROM sales_records 
            WHERE sale_date >= datetime('now','-6 months')
            GROUP BY month ORDER BY month
        """).fetchall()
    stats['monthly_sales'] = [dict(row) for row in monthly]
    
    return jsonify(stats)


# ─── API: Update Product Price ───────────────────────────────────
@app.route('/api/product/<int:product_id>/price', methods=['POST'])
def api_update_product_price(product_id):
    """更新商品售价（用于库存列表内联编辑）"""
    data = request.json or {}
    new_price = data.get('selling_price')
    
    if new_price is None:
        return jsonify({'success': False, 'error': '販売価格が必要です'}), 400
    
    try:
        new_price = int(new_price)
        if new_price < 0:
            return jsonify({'success': False, 'error': '価格は0以上でなければなりません'}), 400
    except (ValueError, TypeError):
        return jsonify({'success': False, 'error': '有効な数値を入力してください'}), 400
    
    db = get_db()
    product = db.execute("SELECT id FROM products WHERE id=?", (product_id,)).fetchone()
    if not product:
        return jsonify({'success': False, 'error': '商品が見つかりません'}), 404
    
    db.execute("UPDATE products SET selling_price=?, updated_at=? WHERE id=?",
              (new_price, datetime.now(), product_id))
    
    app.logger.info(f'[PriceUpdate] Product {product_id}: price changed to ¥{new_price}')
    return jsonify({'success': True, 'new_price': new_price})


@app.route('/api/product/<int:product_id>/adjust-stock', methods=['POST'])
def api_adjust_stock(product_id):
    """直接在庫数を調整（差分をinbound_recordとして記録）"""
    db = get_db()
    product = db.execute("SELECT id, product_name FROM products WHERE id=?", (product_id,)).fetchone()
    if not product:
        return jsonify({'success': False, 'error': '商品が見つかりません'}), 404
    
    data = request.get_json()
    target_stock = data.get('target_stock', None)
    if target_stock is None:
        return jsonify({'success': False, 'error': '目標の在庫数を指定してください'}), 400
    
    try:
        target_stock = int(target_stock)
    except (ValueError, TypeError):
        return jsonify({'success': False, 'error': '有効な数値を入力してください'}), 400
    
    # Calculate current stock
    total_in = db.execute("SELECT COALESCE(SUM(qty),0) FROM inbound_records WHERE product_id=?", (product_id,)).fetchone()[0]
    total_out = db.execute("SELECT COALESCE(SUM(qty),0) FROM sales_records WHERE product_id=?", (product_id,)).fetchone()[0]
    current_stock = total_in - total_out
    
    diff = target_stock - current_stock
    if diff == 0:
        return jsonify({'success': True, 'message': '在庫数に変更はありません', 'stock': current_stock})
    
    # Create adjustment inbound record
    note = '在庫調整' if diff >= 0 else '在庫調整（減少）'
    db.execute(
        "INSERT INTO inbound_records (product_id, qty, unit_cost, inbound_date, notes) VALUES (?, ?, ?, ?, ?)",
        (product_id, diff, product['unit_cost'] if 'unit_cost' in dict(product) else 0, datetime.now(), note)
    )
    
    # Update product status
    new_stock = total_in + diff - total_out
    new_status = 'sold_out' if new_stock <= 0 else 'in_stock'
    db.execute("UPDATE products SET status=?, updated_at=? WHERE id=?", (new_status, datetime.now(), product_id))
    
    app.logger.info(f'[StockAdjust] Product {product_id} "{product["product_name"]}": {current_stock} → {new_stock} (diff={diff:+d})')
    return jsonify({'success': True, 'stock': new_stock, 'diff': diff, 'message': f'在庫数を {current_stock} → {new_stock} に調整しました'})


@app.route('/api/product/<int:product_id>/adjust-inbound', methods=['POST'])
def api_adjust_inbound(product_id):
    """目標の入庫数に合わせてinbound_recordsを調整"""
    db = get_db()
    product = db.execute("SELECT id, product_name, unit_cost FROM products WHERE id=?", (product_id,)).fetchone()
    if not product:
        return jsonify({'success': False, 'error': '商品が見つかりません'}), 404

    data = request.get_json()
    target_inbound = data.get('target_inbound', None)
    if target_inbound is None:
        return jsonify({'success': False, 'error': '目標の入庫数を指定してください'}), 400

    try:
        target_inbound = int(target_inbound)
        if target_inbound < 0:
            return jsonify({'success': False, 'error': '0以上の数値を入力してください'}), 400
    except (ValueError, TypeError):
        return jsonify({'success': False, 'error': '有効な数値を入力してください'}), 400

    total_in = db.execute("SELECT COALESCE(SUM(qty),0) FROM inbound_records WHERE product_id=?", (product_id,)).fetchone()[0]
    diff = target_inbound - total_in

    if diff == 0:
        total_out = db.execute("SELECT COALESCE(SUM(qty),0) FROM sales_records WHERE product_id=?", (product_id,)).fetchone()[0]
        return jsonify({'success': True, 'message': '入庫数に変更はありません', 'total_in': total_in, 'current_stock': total_in - total_out})

    # 差分を入庫調整レコードとして記録
    note = '入庫数調整' if diff >= 0 else '入庫数調整（減少）'
    db.execute(
        "INSERT INTO inbound_records (product_id, qty, unit_cost, inbound_date, notes) VALUES (?, ?, ?, ?, ?)",
        (product_id, diff, product['unit_cost'] or 0, datetime.now(), note)
    )

    new_total_in = total_in + diff
    total_out = db.execute("SELECT COALESCE(SUM(qty),0) FROM sales_records WHERE product_id=?", (product_id,)).fetchone()[0]
    new_stock = new_total_in - total_out
    new_status = 'sold_out' if new_stock <= 0 else 'in_stock'
    db.execute("UPDATE products SET status=?, updated_at=? WHERE id=?", (new_status, datetime.now(), product_id))

    app.logger.info(f'[InboundAdjust] Product {product_id} "{product["product_name"]}": 入庫数 {total_in}→{new_total_in} (diff={diff:+d}), 在庫 {new_stock}')
    return jsonify({
        'success': True,
        'total_in': new_total_in,
        'current_stock': new_stock,
        'diff': diff,
        'message': f'入庫数を {total_in} → {new_total_in} に調整しました'
    })


@app.route('/api/product/<int:product_id>/update', methods=['POST'])
def api_update_product(product_id):
    """直接编辑已入库商品（商品名/进价/售价/品类/照片）"""
    db = get_db()
    product = db.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    if not product:
        return jsonify({'success': False, 'error': '商品が見つかりません'}), 404

    # 兼容 JSON 和 FormData（multipart/form-data）两种请求方式
    if request.is_json:
        data = request.json or {}
    else:
        data = {k: v for k, v in request.form.items()}
    updated_fields = []
    updated_values = []

    # 商品名
    if 'product_name' in data and data['product_name']:
        updated_fields.append("product_name=?")
        updated_values.append(data['product_name'])

    # 售价
    if 'selling_price' in data and data['selling_price'] is not None:
        try:
            sp = int(data['selling_price'])
            if sp >= 0:
                updated_fields.append("selling_price=?")
                updated_values.append(sp)
        except (ValueError, TypeError):
            pass

    # 进价
    if 'unit_cost' in data and data['unit_cost'] is not None:
        try:
            uc = int(data['unit_cost'])
            if uc >= 0:
                updated_fields.append("unit_cost=?")
                updated_values.append(uc)
        except (ValueError, TypeError):
            pass

    # 品类
    if 'category' in data and data['category']:
        updated_fields.append("category=?")
        updated_values.append(data['category'])

    # 销售路径/販売経路
    if 'sales_channel' in data and data['sales_channel']:
        updated_fields.append("sales_channel=?")
        updated_values.append(data['sales_channel'])

    # 照片处理 — 支持 multipart/form-data 文件上传 和 JSON base64（兼容旧前端）
    photo_filename = None
    photo_encoded = None  # base64 for DB storage
    photo_removed = False
    photo_action = request.form.get('photo_action', '') if 'photo_action' in request.form else ''
    
    if not photo_action and 'photo_base64' in data:
        # 兼容旧 JSON base64 方式
        if data['photo_base64'] and str(data['photo_base64']).startswith('data:image/'):
            photo_action = 'update'
            _old_b64 = data['photo_base64']  # 保存以便后续处理
        elif str(data.get('photo_base64', '')) == '__REMOVE__':
            photo_action = 'remove'
    
    if photo_action == 'update':
        # FormData 方式优先：从 request.files 获取照片
        photo_file = request.files.get('photo')
        photo_encoded = None  # base64 for DB storage
        if photo_file and photo_file.filename:
            orig_name = photo_file.filename.rsplit('.', 1)[-1].lower() if '.' in photo_file.filename else 'jpg'
            if orig_name == 'jpeg':
                orig_name = 'jpg'
            photo_filename = f"product_{product_id}_{uuid.uuid4().hex[:8]}.{orig_name}"
            # Read file bytes for DB storage
            file_bytes = photo_file.read()
            photo_encoded = base64.b64encode(file_bytes).decode('utf-8')
            # Disk save (non-critical backup)
            photo_path = UPLOAD_DIR / photo_filename
            UPLOAD_DIR.mkdir(exist_ok=True, parents=True)
            try:
                photo_file.seek(0)
                photo_file.save(str(photo_path))
                file_size = photo_path.stat().st_size if photo_path.exists() else 0
                app.logger.info(f'[ProductUpdate] Photo saved (multipart): {photo_filename} ({file_size} bytes)')
            except Exception as e:
                app.logger.warning(f'[ProductUpdate] Disk save error (non-critical): {e}')
        elif 'photo_base64' in data:
            # 回退到 base64 方式
            try:
                b64_str = str(data['photo_base64'])
                header, encoded = b64_str.split(',', 1)
                ext = header.split('/')[1].split(';')[0]
                if ext == 'jpeg':
                    ext = 'jpg'
                photo_filename = f"product_{product_id}_{uuid.uuid4().hex[:8]}.{ext}"
                photo_encoded = encoded  # Save to DB
                # Disk save (non-critical backup)
                photo_path = UPLOAD_DIR / photo_filename
                UPLOAD_DIR.mkdir(exist_ok=True, parents=True)
                try:
                    with open(photo_path, 'wb') as f:
                        f.write(base64.b64decode(encoded))
                    if photo_path.exists() and photo_path.stat().st_size > 0:
                        app.logger.info(f'[ProductUpdate] Photo saved (base64): {photo_filename} ({photo_path.stat().st_size} bytes)')
                except Exception as e:
                    app.logger.warning(f'[ProductUpdate] Disk save error (non-critical): {e}')
            except Exception as e:
                app.logger.error(f'[ProductUpdate] Photo base64 error: {e}')
                photo_filename = None
                photo_encoded = None
        else:
            app.logger.warning(f'[ProductUpdate] photo_action=update but no file or base64 data')
        
        # 照片保存成功后更新DB（同时清旧照片）
        if photo_filename and photo_encoded:
            db.execute(
                """UPDATE inbound_records SET photo=?, photo_data=? WHERE id = (
                    SELECT id FROM inbound_records WHERE product_id=? ORDER BY id DESC LIMIT 1
                )""",
                (photo_filename, photo_encoded, product_id)
            )
            app.logger.info(f'[ProductUpdate] Photo DB updated for product {product_id}')
    
    elif photo_action == 'remove':
        photo_removed = True
        db.execute(
            "UPDATE inbound_records SET photo=NULL, photo_data=NULL WHERE product_id=?",
            (product_id,)
        )
        app.logger.info(f'[ProductUpdate] Photo removed for product {product_id}')

    # 执行数据库更新
    if updated_fields:
        updated_fields.append("updated_at=?")
        updated_values.append(datetime.now())
        updated_values.append(product_id)
        sql = f"UPDATE products SET {', '.join(updated_fields)} WHERE id=?"
        db.execute(sql, updated_values)
        app.logger.info(f'[ProductUpdate] Product {product_id}: updated fields: {updated_fields}')

    # PostgreSQL autocommit=False，必须显式提交
    if hasattr(db, 'commit'):
        db.commit()

    resp = {'success': True}
    if photo_encoded:
        resp['photo_url'] = f"data:image/jpeg;base64,{photo_encoded}"
    elif photo_filename:
        resp['photo_url'] = f'/uploads/{photo_filename}'
    if photo_action and not photo_filename and not photo_removed:
        resp['photo_error'] = '写真の保存に失敗しました'
    return jsonify(resp)


@app.route('/api/product/<int:product_id>/delete', methods=['POST'])
def api_delete_product(product_id):
    """删除已入库商品，并退回采购清单"""
    db = get_db()
    product = db.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    if not product:
        return jsonify({'success': False, 'error': '商品が見つかりません'}), 404

    supplier_item_id = product.get('supplier_item_id')

    # 收集关联照片路径（删除前获取）
    photos = []
    try:
        rows = db.execute(
            "SELECT photo FROM inbound_records WHERE product_id=? AND photo IS NOT NULL",
            (product_id,)
        ).fetchall()
        for r in rows:
            if r.get('photo'):
                photos.append(r['photo'])
    except Exception:
        pass

    # 删除产品 → inbound_records / sales_records 级联删除
    product_name = product.get('product_name', '')
    internal_code = product.get('internal_code', '')
    try:
        db.execute("DELETE FROM products WHERE id=?", (product_id,))
    except Exception as e:
        app.logger.error(f'[DeleteProduct] Failed to delete product {product_id}: {e}')
        return jsonify({'success': False, 'error': f'削除に失敗しました: {str(e)}'}), 500

    # 退回采购清单：减少 inbound_qty，若归零则重置 matched
    if supplier_item_id:
        # 计算该商品的实际入库数量
        total_qty = db.execute("SELECT COALESCE(SUM(qty),0) FROM inbound_records WHERE product_id=?",
                              (product_id,)).fetchone()[0]
        db.execute(
            "UPDATE supplier_items SET inbound_qty = COALESCE(inbound_qty,0) - ? WHERE id=?",
            (total_qty, supplier_item_id)
        )
        # 如果 inbound_qty <= 0，重置 matched 状态
        db.execute(
            "UPDATE supplier_items SET matched=FALSE, matched_date=NULL WHERE id=? AND COALESCE(inbound_qty,0) <= 0",
            (supplier_item_id,)
        )
        app.logger.info(f'[DeleteProduct] supplier_item {supplier_item_id} inbound_qty decreased by {total_qty}')

    # 清理照片文件
    for fname in photos:
        try:
            fp = UPLOAD_DIR / fname
            if fp.exists():
                fp.unlink()
                app.logger.info(f'[DeleteProduct] Deleted photo: {fname}')
        except Exception as e:
            app.logger.warning(f'[DeleteProduct] Could not delete photo {fname}: {e}')

    app.logger.info(f'[DeleteProduct] Product {product_id} ({internal_code} - {product_name}) deleted; supplier_item returned to list')
    return jsonify({
        'success': True,
        'message': f'「{product_name}」を削除し、仕入リストに戻しました',
        'product_name': product_name,
        'supplier_item_id': supplier_item_id
    })


# ─── API: Labels ────────────────────────────────────────────────
@app.route('/api/labels/generate', methods=['POST'])
def api_labels_generate():
    data = request.json
    product_ids = data.get('product_ids', [])
    
    db = get_db()
    labels = []
    errors = []
    for pid in product_ids:
        try:
            product = db.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
            if not product:
                errors.append({'id': pid, 'error': '商品が見つかりません'})
                continue
            internal_code = product.get('internal_code') or f'NOCODE-{pid}'
            product_name = (product.get('product_name_ja') or product.get('product_name') or f'商品{pid}')
            if isinstance(product_name, str) and len(product_name) > 50:
                product_name = product_name[:50]
            selling_price = product.get('selling_price') or 0
            try:
                qr_data = generate_qrcode_base64(internal_code)
            except Exception as qe:
                errors.append({'id': pid, 'error': f'QR生成失敗: {str(qe)[:80]}'})
                qr_data = ''
            labels.append({
                'id': product['id'],
                'internal_code': internal_code,
                'product_name': product_name,
                'selling_price': selling_price,
                'qr_code': qr_data
            })
        except Exception as e:
            errors.append({'id': pid, 'error': str(e)[:100]})
    
    return jsonify({'labels': labels, 'errors': errors})


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
    
    # Save to DB (include brand)
    existing = db.execute("SELECT id FROM mercari_listings WHERE product_id=? AND status='draft'", (product_id,)).fetchone()
    if existing:
        db.execute("""
            UPDATE mercari_listings SET title=?, description=?, price=?, shipping_method=?, condition=?, brand=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=?
        """, (title, description, price, shipping_method, condition, brand, existing['id']))
    else:
        db.execute("""
            INSERT INTO mercari_listings (product_id, title, description, price, shipping_method, condition, brand)
            VALUES (?,?,?,?,?,?,?)
        """, (product_id, title, description, price, shipping_method, condition, brand))
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
    raw_products = db.execute("SELECT * FROM products WHERE is_high_value=TRUE ORDER BY selling_price DESC").fetchall()
    result = []
    for p in raw_products:
        total_in = db.execute("SELECT COALESCE(SUM(qty),0) FROM inbound_records WHERE product_id=?", (p['id'],)).fetchone()[0]
        total_out = db.execute("SELECT COALESCE(SUM(qty),0) FROM sales_records WHERE product_id=?", (p['id'],)).fetchone()[0]
        current_stock = total_in - total_out
        if current_stock <= 0:
            continue
        d = dict(p)
        d['current_stock'] = current_stock
        d['has_listing'] = bool(db.execute("SELECT id FROM mercari_listings WHERE product_id=?", (p['id'],)).fetchone())
        result.append(d)
    return jsonify(result)


@app.route('/api/mercari/stock')
def api_mercari_stock():
    """获取所有在库商品供煤炉上架（不限高额，以实际库存>0为准）"""
    db = get_db()
    # 关联查询照片 — 不依赖status字段，改用实际库存计算
    raw_products = db.execute("""SELECT p.*, 
        (SELECT photo FROM inbound_records WHERE product_id=p.id AND photo IS NOT NULL AND photo != '' ORDER BY id DESC LIMIT 1) as photo,
        (SELECT photo_data FROM inbound_records WHERE product_id=p.id AND photo_data IS NOT NULL AND photo_data != '' ORDER BY id DESC LIMIT 1) as photo_data 
        FROM products p ORDER BY p.updated_at DESC""").fetchall()
    result = []
    for p in raw_products:
        total_in = db.execute("SELECT COALESCE(SUM(qty),0) FROM inbound_records WHERE product_id=?", (p['id'],)).fetchone()[0]
        total_out = db.execute("SELECT COALESCE(SUM(qty),0) FROM sales_records WHERE product_id=?", (p['id'],)).fetchone()[0]
        current_stock = total_in - total_out
        # 只返回有实际库存的商品（不依赖status字段）
        if current_stock <= 0:
            continue
        d = dict(p)
        d['current_stock'] = current_stock
        d['has_listing'] = bool(db.execute("SELECT id FROM mercari_listings WHERE product_id=?", (p['id'],)).fetchone())
        # 添加照片URL（DB优先）
        if d.get('photo_data'):
            d['photo_url'] = f"data:image/jpeg;base64,{d['photo_data']}"
        elif d.get('photo'):
            d['photo_url'] = f'/uploads/{d["photo"]}'
        else:
            d['photo_url'] = None
        result.append(d)
    return jsonify(result)


# ─── API: Debug - 查看所有商品状态（调试用）─────────────────
@app.route('/api/debug/products')
def api_debug_products():
    """调试用：查看所有商品的status、current_stock、is_high_value"""
    db = get_db()
    products = db.execute("SELECT p.id, p.internal_code, p.product_name, p.status, p.is_high_value, p.sales_channel, " +
        "(SELECT COALESCE(SUM(qty),0) FROM inbound_records WHERE product_id=p.id) as total_in, " +
        "(SELECT COALESCE(SUM(qty),0) FROM sales_records WHERE product_id=p.id) as total_out " +
        "FROM products p ORDER BY p.id").fetchall()
    result = []
    for p in products:
        d = dict(p)
        d['current_stock'] = d['total_in'] - d['total_out']
        result.append(d)
    return jsonify(result)


# ─── API: Sales Report (销售报表，按平台统计) ───────────────
@app.route('/api/sales/report')
def api_sales_report():
    """按平台/日期统计销售额"""
    start_date = request.args.get('start_date', '')
    end_date = request.args.get('end_date', '')
    platform = request.args.get('platform', '')
    no_filter = request.args.get('no_filter', '') == '1'
    
    db = get_db()
    
    # If no_filter, skip date/platform filtering to show all records
    if no_filter:
        try:
            platform_stats = db.execute("""
                SELECT 
                    COALESCE(s.platform, '未分類') as platform,
                    COUNT(*) as sale_count,
                    COALESCE(SUM(s.total_amount),0) as total_revenue,
                    COALESCE(SUM(s.cost_amount),0) as total_cost,
                    COALESCE(SUM(s.shipping_fee),0) as total_shipping_fee,
                    COALESCE(SUM(s.platform_fee),0) as total_platform_fee,
                    COALESCE(SUM(s.other_fee),0) as total_other_fee,
                    COALESCE(SUM(s.profit_amount),0) as total_profit,
                    COALESCE(AVG(NULLIF(s.total_amount,0)/NULLIF(s.qty,0)),0) as avg_price
                FROM sales_records s
                GROUP BY s.platform
                ORDER BY total_revenue DESC
            """).fetchall()
            daily_stats = db.execute("""
                SELECT 
                    """ + ("DATE(s.sale_date)" if USE_POSTGRES else "date(s.sale_date)") + """ as sale_date,
                    COALESCE(s.platform, '未分類') as platform,
                    COUNT(*) as sale_count,
                    COALESCE(SUM(s.total_amount),0) as total_revenue,
                    COALESCE(SUM(s.cost_amount),0) as total_cost,
                    COALESCE(SUM(s.shipping_fee),0) as total_shipping_fee,
                    COALESCE(SUM(s.platform_fee),0) as total_platform_fee,
                    COALESCE(SUM(s.other_fee),0) as total_other_fee,
                    COALESCE(SUM(s.profit_amount),0) as total_profit
                FROM sales_records s
                GROUP BY """ + ("DATE(s.sale_date)" if USE_POSTGRES else "date(s.sale_date)") + """, s.platform
                ORDER BY sale_date DESC
                LIMIT 90
            """).fetchall()
            
            # Individual sales detail for no_filter mode
            if USE_POSTGRES:
                sales_detail = db.execute("""
                    SELECT s.id, s.product_id, p.product_name, p.internal_code,
                           s.qty, s.unit_price, s.total_amount, s.cost_amount,
                           s.shipping_fee, s.platform_fee, s.other_fee, s.profit_amount,
                           s.payment_method, s.platform,
                           TO_CHAR(s.sale_date, 'YYYY-MM-DD HH24:MI') as sale_time,
                           DATE(s.sale_date) as sale_date
                    FROM sales_records s
                    LEFT JOIN products p ON s.product_id = p.id
                    ORDER BY s.sale_date DESC
                    LIMIT 200
                """).fetchall()
            else:
                sales_detail = db.execute("""
                    SELECT s.id, s.product_id, p.product_name, p.internal_code,
                           s.qty, s.unit_price, s.total_amount, s.cost_amount,
                           s.shipping_fee, s.platform_fee, s.other_fee, s.profit_amount,
                           s.payment_method, s.platform,
                           s.sale_date as sale_time,
                           date(s.sale_date) as sale_date
                    FROM sales_records s
                    LEFT JOIN products p ON s.product_id = p.id
                    ORDER BY s.sale_date DESC
                    LIMIT 200
                """).fetchall()
            
            return jsonify({
                'platform_stats': [dict(row) for row in platform_stats],
                'daily_stats': [dict(row) for row in daily_stats],
                'sales_detail': [dict(row) for row in sales_detail]
            })
        except Exception as e:
            return jsonify({
                'error': f'查询失败(全期间): {type(e).__name__}: {str(e)}',
                'platform_stats': [],
                'daily_stats': [],
                'sales_detail': []
            }), 200
    
    where = "1=1"
    params = []
    if start_date:
        if USE_POSTGRES:
            where += " AND DATE(s.sale_date + INTERVAL '9 hours') >= %s"
        else:
            where += " AND date(s.sale_date, '+9 hours') >= ?"
        params.append(start_date)
    if end_date:
        if USE_POSTGRES:
            where += " AND DATE(s.sale_date + INTERVAL '9 hours') <= %s"
        else:
            where += " AND date(s.sale_date, '+9 hours') <= ?"
        params.append(end_date)
    if platform:
        where += " AND s.platform = %s" if USE_POSTGRES else " AND s.platform = ?"
        params.append(platform)
    
    try:
        # 按平台统计
        platform_stats = db.execute(f"""
            SELECT 
                COALESCE(s.platform, '未分類') as platform,
                COUNT(*) as sale_count,
                COALESCE(SUM(s.total_amount),0) as total_revenue,
                COALESCE(SUM(s.cost_amount),0) as total_cost,
                COALESCE(SUM(s.shipping_fee),0) as total_shipping_fee,
                COALESCE(SUM(s.platform_fee),0) as total_platform_fee,
                COALESCE(SUM(s.other_fee),0) as total_other_fee,
                COALESCE(SUM(s.profit_amount),0) as total_profit,
                COALESCE(AVG(NULLIF(s.total_amount,0)/NULLIF(s.qty,0)),0) as avg_price
            FROM sales_records s
            WHERE {where}
            GROUP BY s.platform
            ORDER BY total_revenue DESC
        """, params).fetchall()
        
        # 按日期统计（最近30天）
        if USE_POSTGRES:
            daily_stats = db.execute(f"""
                SELECT 
                    DATE(s.sale_date) as sale_date,
                    COALESCE(s.platform, '未分類') as platform,
                    COUNT(*) as sale_count,
                    COALESCE(SUM(s.total_amount),0) as total_revenue,
                    COALESCE(SUM(s.cost_amount),0) as total_cost,
                    COALESCE(SUM(s.shipping_fee),0) as total_shipping_fee,
                    COALESCE(SUM(s.platform_fee),0) as total_platform_fee,
                    COALESCE(SUM(s.other_fee),0) as total_other_fee,
                    COALESCE(SUM(s.profit_amount),0) as total_profit
                FROM sales_records s
                WHERE {where}
                GROUP BY DATE(s.sale_date), s.platform
                ORDER BY sale_date DESC
                LIMIT 90
            """, params).fetchall()
        else:
            daily_stats = db.execute(f"""
                SELECT 
                    date(s.sale_date) as sale_date,
                    COALESCE(s.platform, '未分類') as platform,
                    COUNT(*) as sale_count,
                    COALESCE(SUM(s.total_amount),0) as total_revenue,
                    COALESCE(SUM(s.cost_amount),0) as total_cost,
                    COALESCE(SUM(s.shipping_fee),0) as total_shipping_fee,
                    COALESCE(SUM(s.platform_fee),0) as total_platform_fee,
                    COALESCE(SUM(s.other_fee),0) as total_other_fee,
                    COALESCE(SUM(s.profit_amount),0) as total_profit
                FROM sales_records s
                WHERE {where}
                GROUP BY date(s.sale_date), s.platform
                ORDER BY sale_date DESC
                LIMIT 90
            """, params).fetchall()
        
        # 个別販売明細（逐条记录）
        if USE_POSTGRES:
            sales_detail = db.execute(f"""
                SELECT s.id, s.product_id, p.product_name, p.internal_code,
                       s.qty, s.unit_price, s.total_amount, s.cost_amount,
                       s.shipping_fee, s.platform_fee, s.other_fee, s.profit_amount,
                       s.payment_method, s.platform,
                       TO_CHAR(s.sale_date, 'YYYY-MM-DD HH24:MI') as sale_time,
                       DATE(s.sale_date) as sale_date
                FROM sales_records s
                LEFT JOIN products p ON s.product_id = p.id
                WHERE {where}
                ORDER BY s.sale_date DESC
                LIMIT 200
            """, params).fetchall()
        else:
            sales_detail = db.execute(f"""
                SELECT s.id, s.product_id, p.product_name, p.internal_code,
                       s.qty, s.unit_price, s.total_amount, s.cost_amount,
                       s.shipping_fee, s.platform_fee, s.other_fee, s.profit_amount,
                       s.payment_method, s.platform,
                       s.sale_date as sale_time,
                       date(s.sale_date) as sale_date
                FROM sales_records s
                LEFT JOIN products p ON s.product_id = p.id
                WHERE {where}
                ORDER BY s.sale_date DESC
                LIMIT 200
            """, params).fetchall()
        
        return jsonify({
            'platform_stats': [dict(row) for row in platform_stats],
            'daily_stats': [dict(row) for row in daily_stats],
            'sales_detail': [dict(row) for row in sales_detail]
        })
    except Exception as e:
        return jsonify({
            'error': f'查询失败: {type(e).__name__}: {str(e)}',
            'platform_stats': [],
            'daily_stats': [],
            'sales_detail': []
        }), 200


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
            FROM supplier_items WHERE matched=FALSE
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
            'message': 'AIレスポンスの解析に失敗しました。手動選択をご利用ください。'
        })
    except Exception as e:
        err_str = str(e)
        # Check for quota/exhausted errors (429)
        if '429' in err_str or 'ResourceExhausted' in err_str or 'quota' in err_str.lower():
            return jsonify({
                'error': 'AIの無料枠の利用制限に達しました。しばらく待つか、手動で選択してください。',
                'type': 'quota_exceeded',
                'message': 'AI無料枠制限中 - 手動で商品を選択できます'
            }), 429
        return jsonify({'error': f'AI認識エラー: {err_str}'}), 500


@app.route('/api/inbound/ai-read-barcode', methods=['POST'])
def api_ai_read_barcode():
    """读取条码：pyzbar多策略解码，不回退到AI（AI配额不可靠）"""
    file = request.files.get('image')
    if not file:
        return jsonify({'error': '画像がありません'}), 400

    try:
        img = Image.open(file.stream)
        
        # ── Strategy 1: Original image ──
        decoded = pyzbar_decode(img)
        if decoded:
            return _barcode_result(decoded, 'pyzbar_original')
        
        # ── Strategy 2: Grayscale ──
        img_gray = img.convert('L')
        decoded = pyzbar_decode(img_gray)
        if decoded:
            return _barcode_result(decoded, 'pyzbar_gray')
        
        # ── Strategy 3: Binarize (binary threshold) ──
        img_bin = img_gray.point(lambda x: 255 if x > 128 else 0, '1')
        decoded = pyzbar_decode(img_bin)
        if decoded:
            return _barcode_result(decoded, 'pyzbar_binary')
        
        # ── Strategy 4: Upscale 2x then grayscale ──
        w, h = img.size
        img_big = img.resize((w * 2, h * 2), Image.LANCZOS).convert('L')
        decoded = pyzbar_decode(img_big)
        if decoded:
            return _barcode_result(decoded, 'pyzbar_upscale2x')
        
        # ── Strategy 5: Upscale 3x then binary ──
        img_big3 = img.resize((w * 3, h * 3), Image.LANCZOS).convert('L')
        img_big3_bin = img_big3.point(lambda x: 255 if x > 128 else 0, '1')
        decoded = pyzbar_decode(img_big3_bin)
        if decoded:
            return _barcode_result(decoded, 'pyzbar_upscale3x')
        
        # ── Strategy 6: Contrast stretch + binary ──
        import numpy as np
        img_arr = np.array(img_gray)
        p2, p98 = np.percentile(img_arr, (2, 98))
        if p98 > p2:
            img_arr = np.clip((img_arr - p2) * 255.0 / (p98 - p2), 0, 255).astype(np.uint8)
        img_contrast = Image.fromarray(img_arr)
        decoded = pyzbar_decode(img_contrast)
        if decoded:
            return _barcode_result(decoded, 'pyzbar_contrast')
        
        # ── Strategy 7: Adaptive threshold (local binarization) ──
        try:
            from PIL import ImageFilter
            blur = img_gray.filter(ImageFilter.GaussianBlur(radius=15))
            img_arr = np.array(img_gray).astype(np.float32)
            blur_arr = np.array(blur).astype(np.float32)
            diff = img_arr - blur_arr
            adaptive = ((diff > 0) * 255).astype(np.uint8)
            img_adaptive = Image.fromarray(adaptive, 'L')
            decoded = pyzbar_decode(img_adaptive)
            if decoded:
                return _barcode_result(decoded, 'pyzbar_adaptive')
        except:
            pass
        
        # ── All strategies failed ──
        print('[pyzbar] 全部7种策略解码失败')
        return jsonify({
            'success': False,
            'error': '条码解码失败。请确保照片清晰、条码完整可见、光线充足。',
            'type': 'decode_failed'
        }), 422
        
    except Exception as e:
        err_str = str(e)
        print(f'[barcode] 异常: {err_str}')
        return jsonify({'error': f'条码读取错误: {err_str}'}), 500


def _barcode_result(decoded_list, method):
    """Helper: extract barcode data from pyzbar result, clean noise"""
    texts = []
    for d in decoded_list:
        raw = d.data.decode('utf-8', errors='replace').strip()
        if raw:
            texts.append(raw)
    if not texts:
        return None
    
    raw_barcode = texts[0]
    print(f'[pyzbar-{method}] 原始解码: "{raw_barcode}"')
    
    # Clean: strip leading/trailing non-digit chars (CODE-128 start/stop artifacts)
    # Also strip FNC1 prefix like "]C1" or "]d2"
    cleaned = re.sub(r'^[^\d\-]*', '', raw_barcode)  # strip leading junk
    cleaned = re.sub(r'[^\d\-]*$', '', cleaned)       # strip trailing junk
    # Remove FNC1/AI prefixes like "]C1", "]e0", "]d2", etc.
    cleaned = re.sub(r'^\]\w\d+', '', cleaned)
    # If after cleaning it's empty, keep original
    if not cleaned or not re.search(r'\d', cleaned):
        cleaned = re.sub(r'[^\d]', '', raw_barcode)  # last resort: digits only
    
    print(f'[pyzbar-{method}] 清理后: "{cleaned}"')
    
    return jsonify({
        'success': True,
        'barcode': cleaned,
        'method': method,
        'raw': raw_barcode,
        'all_barcodes': texts
    })


def _pyzbar_try_decode(img):
    """Run pyzbar multi-strategy decode on a PIL Image, return cleaned barcode or None.
    Optimized for speed: fewer strategies, smaller image sizes."""
    import numpy as np
    
    strategies = [
        # (name, image, desc)
        ('gray', lambda: img.convert('L')),
        ('bin128', lambda: img.convert('L').point(lambda x: 255 if x > 128 else 0, '1')),
        ('bin80', lambda: img.convert('L').point(lambda x: 255 if x > 80 else 0, '1')),
        ('contrast', lambda: _make_contrast(img.convert('L'))),
        ('upscale2x_bin', lambda: img.resize((img.width*2, img.height*2), Image.LANCZOS).convert('L').point(lambda x: 255 if x > 128 else 0, '1')),
    ]
    
    for name, make_img in strategies:
        try:
            proc = make_img()
            decoded = pyzbar_decode(proc)
            if decoded:
                raw = decoded[0].data.decode('utf-8', errors='replace').strip()
                if raw:
                    cleaned = re.sub(r'^[^\d\-]*', '', raw)
                    cleaned = re.sub(r'[^\d\-]*$', '', cleaned)
                    cleaned = re.sub(r'^\]\w\d+', '', cleaned)
                    if cleaned and re.search(r'\d', cleaned):
                        print(f'[scan-frame] {name}: "{raw}" → "{cleaned}"')
                        return cleaned
        except Exception as e:
            print(f'[scan-frame] {name} error: {e}')
    
    return None


def _make_contrast(gray_img):
    """Stretch contrast of a grayscale image."""
    import numpy as np
    arr = np.array(gray_img)
    p5, p95 = np.percentile(arr, (5, 95))
    if p95 > p5:
        arr = np.clip((arr - p5) * 255.0 / (p95 - p5), 0, 255).astype(np.uint8)
    return Image.fromarray(arr)


@app.route('/api/barcode/scan-frame', methods=['POST'])
def api_barcode_scan_frame():
    """Receive a video frame, try to decode barcode with pyzbar. For continuous scanning."""
    file = request.files.get('frame')
    if not file:
        return jsonify({'found': False}), 200
    
    try:
        img = Image.open(file.stream)
        
        # Resize for speed: max 800px wide
        if img.width > 800:
            ratio = 800 / img.width
            img = img.resize((800, int(img.height * ratio)), Image.LANCZOS)
        
        barcode = _pyzbar_try_decode(img)
        
        if barcode:
            return jsonify({'found': True, 'barcode': barcode})
        else:
            return jsonify({'found': False})
    
    except Exception as e:
        return jsonify({'found': False, 'error': str(e)}), 200


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
    db.execute("UPDATE supplier_items SET matched=TRUE, matched_date=CURRENT_TIMESTAMP WHERE id=?", (supplier_item_id,))
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
        query += " AND s.platform = ?" if not USE_POSTGRES else " AND s.platform = %s"
        params.append(platform)
    
    if date:
        if USE_POSTGRES:
            query += " AND DATE(s.sale_date + INTERVAL '9 hours') = %s"
        else:
            query += " AND date(s.sale_date, '+9 hours') = ?"
        params.append(date)
    
    query += " ORDER BY s.sale_date DESC LIMIT ?"
    params.append(limit)
    
    sales = db.execute(query, params).fetchall()
    return jsonify([dict(row) for row in sales])


@app.route('/api/sales/today')
def api_sales_today():
    """本日销售汇总（JST时区）"""
    db = get_db()
    today = jst_today()
    if USE_POSTGRES:
        summary = db.execute("""
            SELECT COUNT(*) as total_count,
                   COALESCE(SUM(total_amount),0) as total_revenue,
                   COALESCE(SUM(profit_amount),0) as total_profit
            FROM sales_records WHERE DATE(sale_date + INTERVAL '9 hours') = %s
        """, (today,)).fetchone()
        platform_stats_rows = db.execute("""
            SELECT 
                COALESCE(s.platform, '未分類') as platform,
                COUNT(*) as count,
                COALESCE(SUM(s.total_amount),0) as revenue
            FROM sales_records s
            WHERE DATE(s.sale_date + INTERVAL '9 hours') = %s
            GROUP BY s.platform
        """, (today,)).fetchall()
    else:
        summary = db.execute("""
            SELECT COUNT(*) as total_count,
                   COALESCE(SUM(total_amount),0) as total_revenue,
                   COALESCE(SUM(profit_amount),0) as total_profit
            FROM sales_records WHERE date(sale_date, '+9 hours') = ?
        """, (today,)).fetchone()
        platform_stats_rows = db.execute("""
            SELECT 
                COALESCE(s.platform, '未分類') as platform,
                COUNT(*) as count,
                COALESCE(SUM(s.total_amount),0) as revenue
            FROM sales_records s
            WHERE date(s.sale_date, '+9 hours') = ?
            GROUP BY s.platform
        """, (today,)).fetchall()
    
    platform_stats = {}
    for row in platform_stats_rows:
        d = dict(row)
        platform_stats[d['platform']] = {'count': d['count'], 'revenue': d['revenue']}
    
    result = dict(summary)
    result['platform_stats'] = platform_stats
    return jsonify(result)


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


@app.route('/api/sales/<int:sale_id>/update-fees', methods=['POST'])
def api_sales_update_fees(sale_id):
    """更新销售记录的费用并重算利润"""
    db = get_db()
    data = request.get_json()
    
    shipping_fee = int(data.get('shipping_fee', 0) or 0)
    platform_fee = int(data.get('platform_fee', 0) or 0)
    other_fee = int(data.get('other_fee', 0) or 0)
    
    # Get existing record to recalculate profit
    sale = db.execute(
        "SELECT total_amount, cost_amount FROM sales_records WHERE id=?", (sale_id,)
    ).fetchone()
    if not sale:
        return jsonify({'error': '販売記録が見つかりません'}), 404
    
    profit_amount = sale[0] - (sale[1] or 0) - shipping_fee - platform_fee - other_fee
    
    db.execute("""
        UPDATE sales_records SET 
            shipping_fee = ?, platform_fee = ?, other_fee = ?,
            profit_amount = ?
        WHERE id = ?
    """, (shipping_fee, platform_fee, other_fee, profit_amount, sale_id))
    db.commit()
    
    return jsonify({
        'success': True,
        'sale_id': sale_id,
        'shipping_fee': shipping_fee,
        'platform_fee': platform_fee,
        'other_fee': other_fee,
        'total_fees': shipping_fee + platform_fee + other_fee,
        'profit_amount': profit_amount
    })


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
        if USE_POSTGRES:
            query += " AND DATE(p.scrapped_at) >= %s"
        else:
            query += " AND date(p.scrapped_at) >= ?"
        params.append(start_date)
    if end_date:
        if USE_POSTGRES:
            query += " AND DATE(p.scrapped_at) <= %s"
        else:
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


# ─── Static files (uploads) ────────────────────────────────────
@app.route('/uploads/<path:filename>')
def serve_upload(filename):
    import mimetypes
    # 安全检查：防止路径穿越攻击
    safe_filename = os.path.basename(filename)
    file_path = UPLOAD_DIR / safe_filename
    
    if not file_path.exists():
        app.logger.warning(f'[Upload] File not found: {file_path}')
        return jsonify({'error': 'File not found'}), 404
    
    # 检查文件大小（0字节或过小可能是损坏的）
    file_size = file_path.stat().st_size
    if file_size == 0:
        app.logger.error(f'[Upload] File is empty: {file_path}')
        return jsonify({'error': 'File is empty (corrupted)'}), 500
    
    # 自动检测MIME类型
    mime_type = mimetypes.guess_type(str(file_path))[0] or 'application/octet-stream'
    app.logger.info(f'[Upload] Serving: {safe_filename} ({file_size} bytes, {mime_type})')
    
    return send_from_directory(str(UPLOAD_DIR), safe_filename, mimetype=mime_type)


@app.route('/api/debug/photo-check/<int:product_id>')
def api_debug_photo_check(product_id):
    """检查指定商品的照片文件状态"""
    db = get_db()
    result = db.execute("SELECT photo FROM inbound_records WHERE product_id=? ORDER BY id DESC LIMIT 1", (product_id,)).fetchone()
    
    if not result or not result.get('photo'):
        return jsonify({'found': False, 'message': '写真が見つかりません'})
    
    photo_name = result['photo']
    photo_path = UPLOAD_DIR / photo_name
    
    info = {
        'photo_name': photo_name,
        'photo_url': f'/uploads/{photo_name}',
        'file_exists': photo_path.exists(),
        'UPLOAD_dir': str(UPLOAD_DIR),
        'UPLOAD_exists': UPLOAD_DIR.exists()
    }
    
    if photo_path.exists():
        stat = photo_path.stat()
        info['file_size'] = stat.st_size
        info['is_empty'] = stat.st_size == 0
        # 读取前几个字节判断是否是有效图片
        with open(photo_path, 'rb') as f:
            header = f.read(8)
        info['header_hex'] = header.hex()
        # 常见图片头: JPEG=ffd8, PNG=89504e47, GIF=47494638
        if header[:2] == b'\xff\xd8':
            info['format'] = 'JPEG (valid)'
        elif header[:4] == b'\x89PNG':
            info['format'] = 'PNG (valid)'
        elif header[:2] == b'GI':
            info['format'] = 'GIF (valid)'
        else:
            info['format'] = f'UNKNOWN (possibly corrupted!)'
            info['warning'] = 'ファイルが画像形式ではありません！再アップロードが必要です。'
    else:
        info['message'] = f'ファイルが存在しません: {photo_path}'
    
    return jsonify(info)


@app.route('/api/debug/data-check')
def api_debug_data_check():
    """诊断端点：检查数据库各表含有多少数据"""
    db = get_db()
    tables = ['supplier_imports', 'supplier_items', 'products', 'inbound_records', 'sales_records', 'mercari_listings']
    result = {}
    for table in tables:
        try:
            count = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            result[table] = count
        except Exception as e:
            result[table] = f"ERROR: {e}"
    
    result['db_mode'] = 'PostgreSQL' if USE_POSTGRES else 'SQLite'
    
    if not USE_POSTGRES:
        try:
            result['db_path'] = str(DB_PATH)
            result['db_exists'] = DB_PATH.exists()
            if DB_PATH.exists():
                result['db_size'] = DB_PATH.stat().st_size
        except:
            pass
    
    return jsonify(result)


@app.route('/api/debug/fs-test')
def api_debug_fs_test():
    """诊断端点：测试文件系统是否可写（照片保存的前提条件）"""
    import tempfile
    results = {
        'UPLOAD_DIR': str(UPLOAD_DIR),
        'UPLOAD_dir_exists': UPLOAD_DIR.exists(),
    }

    # 测试1: 目录是否存在且可访问
    if UPLOAD_DIR.exists():
        try:
            results['UPLOAD_dir_readable'] = os.access(str(UPLOAD_DIR), os.R_OK)
            results['UPLOAD_dir_writable'] = os.access(str(UPLOAD_DIR), os.W_OK)
            # 列出现有文件
            try:
                files = list(UPLOAD_DIR.iterdir())
                results['existing_files'] = [{'name': f.name, 'size': f.stat().st_size} for f in files if f.is_file()][:10]
            except:
                results['existing_files'] = []
        except Exception as e:
            results['UPLOAD_access_error'] = str(e)

    # 测试2: 尝试创建目录
    try:
        UPLOAD_DIR.mkdir(exist_ok=True, parents=True)
        results['mkdir_ok'] = True
    except Exception as e:
        results['mkdir_ok'] = False
        results['mkdir_error'] = str(e)

    # 测试3: 尝试写入测试文件
    test_filename = f"fs_test_{uuid.uuid4().hex[:6]}.txt"
    test_path = UPLOAD_DIR / test_filename
    try:
        with open(test_path, 'w') as f:
            f.write('filesystem write test')
        results['write_test'] = 'OK'
        results['test_file_exists'] = test_path.exists()
        if test_path.exists():
            results['test_file_size'] = test_path.stat().st_size
        # 清理测试文件
        try:
            test_path.unlink()
            results['cleanup'] = 'OK'
        except:
            results['cleanup'] = 'FAILED (test file left behind)'
    except PermissionError as e:
        results['write_test'] = 'PERMISSION_DENIED'
        results['write_error'] = str(e)
    except OSError as e:
        results['write_test'] = 'OS_ERROR'
        results['write_error'] = str(e)
    except Exception as e:
        results['write_test'] = 'ERROR'
        results['write_error'] = f"{type(e).__name__}: {e}"

    # 测试4: 检查Railway volume挂载
    for check_path in ['/data', '/app/data', '/tmp', '/app/uploads']:
        p = Path(check_path)
        results[f'path_{check_path}_exists'] = p.exists()

    return jsonify(results)


@app.route('/debug-photos')
def debug_photos():
    """诊断端点：检查照片文件和URL"""
    db = get_db()
    
    # 查询所有有照片的记录
    photos = db.execute("""
        SELECT p.id, p.internal_code, p.product_name, 
               (SELECT photo FROM inbound_records WHERE product_id=p.id ORDER BY id DESC LIMIT 1) as photo
        FROM products p
        WHERE EXISTS (SELECT 1 FROM inbound_records WHERE product_id=p.id AND photo IS NOT NULL AND photo != '')
    """).fetchall()
    
    result = {
        'upload_dir': str(UPLOAD_DIR),
        'upload_dir_exists': UPLOAD_DIR.exists(),
        'photos': []
    }
    
    if UPLOAD_DIR.exists():
        result['files_in_upload_dir'] = [f.name for f in UPLOAD_DIR.iterdir() if f.is_file()]
    
        # 检查每个照片文件
        for p in photos:
            photo_info = dict(p)
            photo_filename = photo_info.get('photo')
            if photo_filename:
                photo_path = UPLOAD_DIR / photo_filename
                photo_info['photo_url'] = f'/uploads/{photo_filename}'
                photo_info['file_exists'] = photo_path.exists()
                if photo_path.exists():
                    photo_info['file_size'] = photo_path.stat().st_size
            result['photos'].append(photo_info)
    
    # 生成HTML报告
    html = '''<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>写真诊断</title>
<style>
body{font-family:sans-serif;padding:20px;max-width:1200px;margin:0 auto}
.card{background:#fff;border-radius:12px;padding:16px;margin-bottom:16px;box-shadow:0 1px 3px rgba(0,0,0,.1)}
.ok{color:green}.error{color:red}.warn{color:orange}
table{width:100%;border-collapse:collapse;margin-top:12px}
th,td{border:1px solid #e5e7eb;padding:8px 12px;text-align:left}
th{background:#f9fafb;font-weight:600}
</style>
</head>
<body>
<h2>写真診断</h2>
'''
    
    html += f'<div class="card"><h4>UPLOAD_DIR</h4>'
    html += f'<p>路径: <code>{result["upload_dir"]}</code></p>'
    html += f'<p>存在: <span class="{"ok" if result["upload_dir_exists"] else "error"}">{result["upload_dir_exists"]}</span></p>'
    if result['upload_dir_exists'] and 'files_in_upload_dir' in result:
        html += f'<p>文件数: {len(result["files_in_upload_dir"])}</p>'
        if result['files_in_upload_dir']:
            html += '<p>文件列表:</p><ul>'
            for f in result['files_in_upload_dir'][:20]:
                html += f'<li><code>{f}</code></li>'
            if len(result['files_in_upload_dir']) > 20:
                html += f'<li>... 还有 {len(result["files_in_upload_dir"]) - 20} 个文件</li>'
            html += '</ul>'
    html += '</div>'
    
    if result['photos']:
        html += '<div class="card"><h4>有写真の商品</h4>'
        html += '''<table>
            <tr><th>ID</th><th>内部コード</th><th>商品名</th><th>写真ファイル</th><th>URL</th><th>ファイル存在</th><th>サイズ</th></tr>
        '''
        for p in result['photos']:
            cls = 'ok' if p.get('file_exists') else 'error'
            html += f'''<tr>
                <td>{p.get('id')}</td>
                <td><code>{p.get('internal_code')}</code></td>
                <td>{(p.get('product_name') or '')[:50]}</td>
                <td><code>{p.get('photo')}</code></td>
                <td><a href="{p.get('photo_url')}" target="_blank">{p.get('photo_url')}</a></td>
                <td class="{cls}">{p.get('file_exists', False)}</td>
                <td>{p.get('file_size', '-')}</td>
            </tr>'''
        html += '</table></div>'
    else:
        html += '<div class="card"><p class="warn">写真がある商品がありません。入庫時に写真をアップロードしてください。</p></div>'
    
    html += '<p style="margin-top:20px"><a href="/inventory">在庫一覧に戻る</a></p>'
    html += '</body></html>'
    
    return html


# ─── Main ───────────────────────────────────────────────────────
if __name__ == '__main__':
    init_db()
    print("\n========================================")
    print("  托盘杂货进销存管理系统")
    print("  http://localhost:5000")
    print("========================================\n")
    app.run(debug=True, host='0.0.0.0', port=5000)
