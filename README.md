# 托盘杂货进销存管理系统

Flask-based inventory management system for Japanese cross-border return package sales business.

## Features
- Import supplier Excel lists (300-400 items per pallet)
- Barcode scanning / photo recognition / manual input for inbound
- Internal QR code label generation
- Sales management (physical store / Mercari / Yahoo / Rakuten)
- Product scrap management (counterfeit/damaged items)
- External sales registration
- Sales reports by platform

## Tech Stack
- Backend: Flask + SQLite
- Frontend: Bootstrap 5 + Japanese UI
- Barcode: pyzbar + qrcode
- Deploy: Railway (free)

## Installation

```bash
pip install -r requirements.txt
python run.py
```

## Usage

Access at `http://localhost:5000` or deployed URL.
