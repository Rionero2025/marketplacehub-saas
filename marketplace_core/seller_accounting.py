"""Operational accounting adapter; calculations and overrides stay in the legacy service."""
from datetime import date

from services import accounting, dashboard, db
from services.profit_sharing import seller_profit_settings, split_profit


SAFE_COLUMNS = tuple(dict.fromkeys((
    'id', *dashboard.DASHBOARD_ACCOUNTING_COLUMNS.split(','),
    *accounting.ACCOUNTING_INLINE_EDIT_FIELDS, 'sale_original_eur', 'cost_source', 'financial_source', 'country_code',
)))


def account_scope(seller_id: int, account_id: int):
    seller = db.row('SELECT id,name,our_profit_pct,partner_profit_pct FROM sellers WHERE id=? AND active=1', (seller_id,))
    account = db.row('SELECT id,seller_id,marketplace,account_name,active FROM marketplace_accounts WHERE id=? AND seller_id=?', (account_id, seller_id))
    if not seller or not account:
        raise LookupError('Account non disponibile per questo Seller.')
    return seller, account


def filtered_rows(seller_id: int, account_id: int, date_from: date, date_to: date,
                  search: str = '', missing_only: bool = False, *, suppliers=(), statuses=(), countries=(), facets=None):
    seller, account = account_scope(seller_id, account_id)
    columns = ','.join(SAFE_COLUMNS)
    records = db.rows(f'SELECT {columns} FROM accounting_order_lines WHERE seller_id=? AND marketplace_account_id=? AND marketplace=? ORDER BY order_created DESC,id DESC',
                      (seller_id, account_id, account['marketplace']))
    records = accounting.apply_accounting_manual_overrides(records)
    settings = seller_profit_settings(seller)
    selected = []
    for item in records:
        day = dashboard.order_local_date(item.get('order_created'))
        if day is None or not date_from <= day <= date_to:
            continue
        supplier = str(item.get('supplier') or '').strip()
        raw_status = str(item.get('raw_status') or '').strip()
        country = str(item.get('country_code') or '').strip()
        if facets is not None:
            if supplier: facets.setdefault('suppliers', set()).add(supplier)
            if country: facets.setdefault('countries', set()).add(country)
            if raw_status or item.get('status_label'):
                facets.setdefault('statuses', {})[raw_status] = str(item.get('status_label') or raw_status)
        if suppliers and supplier not in suppliers:
            continue
        if statuses and raw_status not in statuses:
            continue
        if countries and country not in countries:
            continue
        if search.strip() and search.strip().casefold() not in ' '.join(str(item.get(k) or '') for k in ('order_id','product_title','ean','composite_sku','supplier','customer_name','tracking','supplier_order_number','note')).casefold():
            continue
        item = dict(item)
        item['edit_values'] = {k: item.get(k) for k in accounting.ACCOUNTING_INLINE_EDIT_FIELDS}
        # Display the same zero-economics rule used by totals and the XLSX exporter.
        if accounting._must_zero_economics(item.get('raw_status'), item.get('status_label'), item.get('note'), item.get('supplier_order_number')):
            item = accounting._zero_economic_record(item)
        item.update(accounting.computed_profit_values(item, settings['our_pct'], settings['partner_pct']))
        item['order_date'] = day.isoformat()
        if not missing_only or item['net_revenue_eur'] is None:
            selected.append(item)
    selected.sort(key=lambda r:(r['order_date'], int(r['id'])), reverse=True)
    return seller, account, selected


def list_rows(seller_id: int, account_id: int, date_from: date, date_to: date,
              search: str = '', missing_only: bool = False, offset: int = 0, limit: int = 50,
              *, suppliers=(), statuses=(), countries=()):
    facets = {}
    seller, account, records = filtered_rows(seller_id, account_id, date_from, date_to, search, missing_only,
                                            suppliers=suppliers, statuses=statuses, countries=countries, facets=facets)
    totals = accounting.totals(records)
    settings = seller_profit_settings(seller)
    shares = split_profit(totals['net_revenue'], settings['our_pct'], settings['partner_pct'])
    return {'items': records[offset:offset+limit], 'total': len(records), 'offset': offset, 'limit': limit,
            'seller': {'id': seller['id'], 'name': seller['name']}, 'profit_split': shares,
            'filter_options': {'suppliers': sorted(facets.get('suppliers', ())), 'countries': sorted(facets.get('countries', ())),
                               'statuses': [{'value': value, 'label': label} for value,label in sorted(facets.get('statuses', {}).items(), key=lambda item:item[1].casefold())]},
            'totals': totals, 'missing_rows': sum(r['net_revenue_eur'] is None for r in records),
            'account': account, 'editable_fields': accounting.ACCOUNTING_INLINE_EDIT_FIELDS}


def save_row(seller_id: int, account_id: int, row_id: int, fields: dict, expected: dict):
    _, account = account_scope(seller_id, account_id)
    item = db.row('SELECT row_key FROM accounting_order_lines WHERE id=? AND seller_id=? AND marketplace_account_id=? AND marketplace=?',
                  (row_id, seller_id, account_id, account['marketplace']))
    if not item:
        raise LookupError('Riga contabile non disponibile.')
    result = accounting.save_accounting_inline_edits([{'marketplace_account_id': account_id,
        'marketplace': account['marketplace'], 'row_key': item['row_key'], 'fields': fields, 'expected': expected}], seller_id=seller_id)
    if not result['updated_rows']:
        raise LookupError('Riga contabile non disponibile.')
    return result


def catalog_selection(seller_id: int):
    result = accounting.accounting_catalog_selection(seller_id)
    # URLs and local/storage paths can contain integration credentials.
    result['options'] = [{k: row.get(k) for k in ('price_list_id','supplier_name','list_name','updated_at','source_kind')} for row in result['options']]
    return result
