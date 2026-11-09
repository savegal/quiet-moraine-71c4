

def test_csv_export_escapes_formulas():
    from proxima_bot.admin_panel import csv_safe
    assert csv_safe('=HYPERLINK("bad")').startswith("'=")
    assert csv_safe('  +123').startswith("'  +")
    assert csv_safe('normal_user')=='normal_user'
