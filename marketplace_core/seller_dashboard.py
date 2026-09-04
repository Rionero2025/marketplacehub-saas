"""Seller-scoped SaaS adapter around the unchanged legacy dashboard formulas."""
from collections import defaultdict
from datetime import date, datetime, timezone

from services import accounting, dashboard, db, product_stats
from services.profit_sharing import seller_profit_settings, split_profit


def load_source(seller_id: int, account_id: int | None = None):
    seller = db.row('SELECT id,name,legal_name,our_profit_pct,partner_profit_pct FROM sellers WHERE id=? AND active=1', (seller_id,))
    if not seller:
        raise LookupError('Seller non disponibile.')
    accounts = db.rows('SELECT id,seller_id,marketplace,account_name,active FROM marketplace_accounts WHERE seller_id=? ORDER BY marketplace,account_name,id', (seller_id,))
    if account_id is not None and account_id not in {int(a['id']) for a in accounts}:
        raise LookupError('Account non disponibile per questo Seller.')
    # Preserve legacy date parsing (including Italian dates and Rome midnight).
    # Only lightweight accounting columns enter memory; never raw JSON/credentials.
    columns = ','.join('l.'+column for column in dashboard.DASHBOARD_ACCOUNTING_COLUMNS.split(','))
    sql = f'''SELECT {columns} FROM accounting_order_lines l
              JOIN marketplace_accounts a ON a.id=l.marketplace_account_id AND a.seller_id=l.seller_id
              WHERE l.seller_id=?'''
    params = [seller_id]
    if account_id is not None:
        sql += ' AND l.marketplace_account_id=?'
        params.append(account_id)
    sql += ' ORDER BY l.order_created,l.id'
    records = accounting.apply_accounting_manual_overrides(db.rows(sql, tuple(params)))
    return seller, accounts, records


def summarize(seller: dict, records: list[dict], start: date, end: date):
    summary = dashboard.date_range_totals(records, start, end)
    settings = seller_profit_settings(seller)
    summary.update(split_profit(summary['profit'], settings['our_pct'], settings['partner_pct']))
    return summary


def snapshot(seller_id: int, *, date_from: date, date_to: date, account_id: int | None = None,
             view: str = 'lines', search: str = '', offset: int = 0, limit: int = 50) -> dict:
    if date_to < date_from:
        raise ValueError('La data finale deve essere successiva o uguale a quella iniziale.')
    seller, accounts, records = load_source(seller_id, account_id)
    details = dashboard.seller_dashboard_detail_rows(seller, records, date_from=date_from, date_to=date_to)
    previous_from, previous_to = product_stats.previous_period_range(date_from, date_to)
    previous = summarize(seller, records, previous_from, previous_to)
    summary = summarize(seller, records, date_from, date_to)
    products = product_stats.sort_product_stats(product_stats.aggregate_product_stats(product_stats.filter_product_rows(details)), 'Più venduti (quantità)')[:10]
    daily = defaultdict(list)
    for item in details:
        daily[item['order_date']].append(item)
    trend = [{'date': day, 'orders': len(dashboard.dashboard_order_detail_rows(items)),
              'sales': round(sum(float(i['sale_eur'] or 0) for i in items),2),
              'profit': round(sum(float(i['net_revenue_eur'] or 0) for i in items),2),
              'missing_profit_rows': len(dashboard.dashboard_missing_detail_rows(items))}
             for day, items in sorted(daily.items())]
    selected = (dashboard.dashboard_missing_detail_rows(details) if view == 'missing' else
                dashboard.dashboard_order_detail_rows(details) if view == 'orders' else details)
    if search.strip():
        query = search.casefold().strip()
        selected = [item for item in selected if query in ' '.join(str(item.get(k) or '') for k in ('order_id','product_title','ean','composite_sku','supplier')).casefold()]
    selected = sorted(selected, key=lambda i: (i.get('order_date') or date.min, str(i.get('order_id') or ''), str(i.get('row_key') or '')), reverse=True)
    return {
        'seller_id': seller_id, 'account_id': account_id, 'date_from': date_from, 'date_to': date_to,
        'timezone': dashboard.DEFAULT_DASHBOARD_TIMEZONE, 'generated_at': datetime.now(timezone.utc),
        'summary': summary, 'previous': {**previous, 'date_from': previous_from, 'date_to': previous_to},
        'top_products': products, 'trend': trend,
        'details': {'items': selected[offset:offset+limit], 'total': len(selected), 'offset': offset, 'limit': limit, 'view': view},
        'accounts': accounts, 'cached_rows': len(records),
        'last_synced': max((str(r.get('synced_at') or '') for r in records), default=''),
        'undated_rows': sum(dashboard.order_local_date(r.get('order_created')) is None for r in records),
        'source': 'accounting_cache',
    }
