# 托盘杂货进销存系统 — 项目总结

## 项目位置
```
C:\Users\Administrator\WorkBuddy\2026-06-27-23-22-25\inventory-system\
```

## 核心文件（修改时主要找这些）

| 文件 | 用途 |
|------|------|
| `app.py` | 后端 Flask 主程序，所有 API + 数据库操作 |
| `templates/sales.html` | 销售页面（POS收银、购物篮、扫码） |
| `templates/inventory.html` | 在库管理（商品列表、销售路径、上架） |
| `templates/inbound.html` | 入库页（扫码匹配、部分入库、进度条） |
| `templates/labels.html` | 标签打印 |
| `templates/mercari.html` | 煤炉出品管理 |
| `templates/base.html` | 基础模板（导航栏、通用样式） |
| `templates/report.html` | 报表/统计页面 |
| `templates/import.html` | 供应商导入 |
| `templates/index.html` | 首页仪表盘 |
| `templates/scrap.html` | 废弃管理 |
| `run.py` | 本地启动入口 |
| `railway.toml` / `Procfile` | Railway 部署配置 |

## 技术栈
- **后端**: Flask (Python) + PostgreSQL（Railway 线上）/ SQLite（本地用 run.py）
- **前端**: Bootstrap 5 + Vanilla JS
- **部署**: GitHub → Railway 自动部署
- **GitHub**: `https://github.com/yaoyutong12/inventory-management-system`

---

## 数据库表结构

### products（商品表）
- `id, internal_code(唯一内部码), product_name, product_name_ja, selling_price, cost_price, current_stock, status, image_path, sales_channel(线下/煤炉/雅虎等), scrap_reason, scrapped_at`

### supplier_imports（供应商导入批次）
- `id, tracking_code, file_name, import_date, status, total_items`

### supplier_items（供应商商品明细）
- `id, import_id, row_index, product_name, sku_code, quantity(expected_qty), unit_price, matching_status, matched_product_id`
- **`inbound_qty`**: 已入库数量（支持部分入库）

### inbound_records（入库记录）
- `id, product_id, tracking_code, qty, created_at, photo, photo_data`

### sales_records（销售记录）
- `id, product_id, qty, unit_price, total_amount, profit, payment_method, platform, created_at, shipping_fee, platform_fee, other_fee`

### mercari_listings（煤炉出品）
- `id, product_id, title, description, price, status, brand, created_at`

### receipts（收据）
- `id, sale_id, receipt_data`

### settings（系统设置）
- `key, value`

---

## 核心功能与关键逻辑

### 1. 销售页面 (`templates/sales.html`)
- **购物篮模式** (`_cartMode` 变量): 扫码连续添加多件商品，统一结算
- **连续扫码**: 使用 `getUserMedia` 摄像头 + BarcodeDetector(浏览器)/pyzbar(服务器) 双策略识别条码
- **去重机制**: `_lastScannedBarcode` / `_lastScanTime`，同一码3秒内不重复处理
- **`onBarcodeFound(barcode, method)`**: 购物篮模式下不停止摄像头，直接将 barcode 作为参数传给 `scanProduct(barcode)`，避免 DOM 竞态
- **`scanProduct(barcodeOverride)`**: 接受可选 barcode 参数，不传则从输入框读取
- **批量结算**: `POST /api/sales/batch-create`，运费/佣金/杂费仅计入第一件

### 2. 入库 (`templates/inbound.html`)
- **部分入库**: `supplier_items.inbound_qty` 记录已入库数量，进度条 X/Y 显示
- **force_new**: 允许同一跟踪号创建多个不同商品
- **未入库列表**: 左侧常驻，显示进度条
- **首次匹配**: `inbound_qty=0` 时也显示 "0/N" 蓝色进度条

### 3. 在库管理 (`templates/inventory.html`)
- **販売経路列**: 彩色 badge 显示（线下/煤炉/雅虎/eBay/Amazon），内联下拉编辑
- **渠道筛选**: 按 sales_channel 过滤
- **上架按钮**: 快捷上架到对应平台

### 4. 煤炉 (`templates/mercari.html`)
- **在库商品查询**: `GET /api/mercari/stock`，使用 `current_stock>0` 判断（不再依赖 status='in_stock'）
- **关键修复**: `result.append(d)` 从 for 循环外移入循环内

### 5. 标签打印 (`templates/labels.html`)
- 每个商品独立 try/except（鲁棒性）
- 前端调试面板

---

## 部署流程
```bash
cd C:\Users\Administrator\WorkBuddy\2026-06-27-23-22-25
git add -A
git commit -m "描述"
git push origin master
# Railway 自动部署
```

## 注意事项
1. **数据库**: app.py 同时兼容 PostgreSQL（线上）和 SQLite（本地 run.py），ALTER TABLE 有双语法
2. **双数据库语法判断**: `if DATABASE_URL` 用 PostgreSQL 的 `ADD COLUMN IF NOT EXISTS`；否则用 SQLite 的 try/except
3. **路径**: 部署在 Railway，本地测试用 `python run.py`
4. **销售汇总 API**: 今日汇总用 `GET /api/sales/today`（不是 /api/sales/report?period=day），返回字段 `total_count/total_revenue/total_profit/platform_stats`
