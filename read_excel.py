import pandas as pd
import sys

f = r'C:\Users\Administrator\WorkBuddy\2026-06-27-23-22-25\supplier_list.xlsx'
try:
    sheets = pd.read_excel(f, sheet_name=None)
    for name, sheet in sheets.items():
        print(f'=== Sheet: {name} ===')
        print(f'Shape: {sheet.shape}')
        print(f'Columns: {list(sheet.columns)}')
        print(sheet.head(30).to_string())
        print()
except Exception as e:
    print(f'Error: {e}', file=sys.stderr)
    import traceback
    traceback.print_exc()
