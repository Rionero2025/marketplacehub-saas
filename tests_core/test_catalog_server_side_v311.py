from pathlib import Path

from marketplace_core.catalogs import CatalogCore


def test_country_cost_mapping_is_server_side_ready():
    core=CatalogCore()
    assert core._country_cost_column('pt')=='cost_pt'
    assert core._country_cost_column('pl')=='cost_zone4'
    assert core._country_cost_column('nl')=='cost_zone3'
    assert core._country_cost_column('')==''


def test_server_side_filters_build_without_loading_frame():
    clause,params,cost_expr=CatalogCore()._query_parts(
        22,search='ABC',min_qty=3,min_cost=10,max_cost=50,
        weight_mode='above',weight_from=5,destination_country='pt',
        positive_cost_only=True,
    )
    assert 'LOWER(ean)' in clause
    assert 'quantity>=?' in clause
    assert 'weight_kg<=0 OR weight_kg<=?' in clause
    assert 'cost_pt' in cost_expr
    assert params[0]==22
    assert '%abc%' in params


def test_working_page_no_longer_loads_entire_catalog():
    page=Path(__file__).resolve().parents[1]/'pages'/'3_Lavora_sui_Listini.py'
    source=page.read_text(encoding='utf-8')
    assert 'load_working_frame' not in source
    assert 'normalize(read_list' not in source
    assert 'catalog_core.query(' in source
    assert 'Prodotti per pagina' in source
