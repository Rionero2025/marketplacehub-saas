from __future__ import annotations

import io
import json
import tracemalloc
from decimal import Decimal

import marketplace_hub_core.catalogs.innpro as innpro
import pytest
from marketplace_hub_core.catalogs.innpro import (
    InnproLimitError,
    InnproValidationError,
    parse_innpro_iof,
)

FULL = b"""<?xml version="1.0" encoding="UTF-8"?>
<offer file_format="IOF" version="3.0"
 xmlns="http://www.iai-shop.com/developers/iof.phtml"
 xmlns:iaiext="http://www.iai-shop.com/developers/iof/extensions.phtml">
 <products language="eng" currency="EUR">
  <product id="123" vat="23.0" type="regular"
    producer_code_standard="GTIN13" code_on_card="HT61">
   <producer id="77" name="Habotest"/>
   <category id="88" name="Gas detectors"/>
   <unit id="1" name="piece"/>
   <series id="3" name="HT Professional"/>
   <warranty id="2" name="24 months"/>
   <card url="https://supplier.example/products/ht61"/>
   <description>
    <name xml:lang="deu">Gasleckdetektor</name>
    <name xml:lang="eng">Mini Gas Leak Detector Habotest HT61</name>
    <long_desc xml:lang="pol">Polski opis</long_desc>
    <long_desc xml:lang="eng"><![CDATA[<p>Complete technical description.</p>]]></long_desc>
    <short_desc xml:lang="por">Detector portatil</short_desc>
    <short_desc xml:lang="eng">Portable gas detector</short_desc>
    <version name="HT61 v2"/>
   </description>
   <price net="25.50"/>
   <srp net="49.99"/>
   <images>
    <large>
     <image iaiext:priority="2" url="https://img.example/side.jpg"/>
     <image iaiext:priority="1" url="https://img.example/main.jpg"
       width="1200" height="1200" hash="abc" date_changed="2026-01-01"/>
    </large>
    <icons><icon url="https://img.example/icon.jpg"/></icons>
   </images>
   <attachments>
    <file version="full" priority="1" attachment_file_type="doc"
      attachment_file_extension="pdf" url="https://docs.example/manual.pdf">
     <name xml:lang="eng">Manual</name>
     <name xml:lang="deu">Handbuch</name>
    </file>
   </attachments>
   <parameters>
    <parameter type="section" id="0" name="Ignored"/>
    <parameter type="parameter" id="1" name="Color">
     <value id="10" name="Black"/><value id="11" name="Red"/>
    </parameter>
    <parameter type="parameter" id="2">
     <name xml:lang="eng">Range</name><value value="ignored">0-10000 ppm</value>
    </parameter>
   </parameters>
   <sizes>
    <size id="1" text_id="one" panel_name="uniw" name="Black"
      code="HT61" code_producer="5907489606738"
      iaiext:code_external="INTERNAL-HT61" weight="4200">
     <price net="26.00"/><stock available_stock_quantity="2"/>
     <stock quantity="3"/>
    </size>
    <size id="2" name="Red" code="HT61-R" code_producer="SUP-R"
      iaiext:code_external="12345678" iaiext:weight_net="500">
     <stock quantity="-1"/><stock stock_quantity="4.9"/>
    </size>
   </sizes>
  </product>
 </products>
</offer>"""


LIGHT = b"""<?xml version="1.0" encoding="UTF-8"?>
<offer file_format="IOF" version="3.0" generated_by="IdoSell"
 xmlns:iof="http://www.iai-shop.com/developers/iof.phtml"
 xmlns:iaiext="http://www.iai-shop.com/developers/iof/extensions.phtml">
 <products currency="EUR">
  <product id="4145">
   <price net="37.70"/><srp net="49.59"/>
   <sizes>
    <size id="0" name="universal" code_producer="SK-100008-11"
      iaiext:code_external="6930460000040" code="4145-0" weight="1030">
     <stock id="0" quantity="-1"/><stock id="1" quantity="140"/>
    </size>
   </sizes>
  </product>
 </products>
</offer>"""


def test_full_feed_preserves_product_content_and_one_row_per_size():
    result = parse_innpro_iof(FULL, expected_role="full")

    assert result.file_format == "iof"
    assert result.feed_role == "full"
    assert result.product_count == 1
    assert len(result.rows) == 2
    first, second = result.rows
    assert first["ean"] == "5907489606738"
    assert first["sku"] == "5907489606738"
    assert first["cost"] == Decimal("26.00")
    assert first["quantity"] == Decimal("5")
    assert second["ean"] == "12345678"
    assert second["sku"] == "SUP-R"
    assert second["cost"] == Decimal("25.50")
    assert second["quantity"] == Decimal("4")

    source = json.loads(first["canonical_json"])["source"]
    assert source["name"] == "Mini Gas Leak Detector Habotest HT61"
    assert source["title_i18n"]["de"] == "Gasleckdetektor"
    assert source["description_i18n"]["pl"] == "Polski opis"
    assert source["short_description_i18n"]["pt"] == "Detector portatil"
    assert source["producer"] == source["brand"] == "Habotest"
    assert source["category"] == "Gas detectors"
    assert source["unit"] == "piece"
    assert source["series"] == "HT Professional"
    assert source["warranty"] == "24 months"
    assert source["version_name"] == "HT61 v2"
    assert source["currency"] == "EUR"
    assert source["vat"] == "23.0"
    assert source["product_type"] == "regular"
    assert source["code_on_card"] == "HT61"
    assert source["producer_code_standard"] == "GTIN13"
    assert source["image_urls"] == [
        "https://img.example/main.jpg",
        "https://img.example/side.jpg",
    ]
    assert source["icon_urls"] == ["https://img.example/icon.jpg"]
    assert source["document_urls"] == ["https://docs.example/manual.pdf"]
    assert source["attachment_metadata"][0]["name_i18n"]["de"] == "Handbuch"
    assert source["parameters"] == {"Color": ["Black", "Red"], "Range": "0-10000 ppm"}
    assert json.loads(first["canonical_json"])["feed_role"] == "full"


def test_light_feed_is_detected_from_content_and_keeps_wholesale_price(tmp_path):
    path = tmp_path / "neutral-name.xml"
    path.write_bytes(LIGHT)

    result = parse_innpro_iof(path, expected_role="light")

    assert result.feed_role == "light"
    assert result.metadata == {
        "version": "3.0",
        "generated_by": "IdoSell",
        "products_currency": "EUR",
    }
    row = result.rows[0]
    assert row["ean"] == "6930460000040"
    assert row["sku"] == "SK-100008-11"
    assert row["cost"] == Decimal("37.70")
    assert row["total_cost"] == Decimal("37.70")
    assert row["quantity"] == Decimal("140")
    source = json.loads(row["canonical_json"])["source"]
    assert source["srp"] == "49.59"
    assert source["weight_g"] == "1030"
    assert source["weight_kg"] == "1.03"


@pytest.mark.parametrize(
    ("payload", "expected_role", "detected"),
    [(FULL, "light", "FULL"), (LIGHT, "full", "LIGHT")],
)
def test_expected_role_mismatch_is_rejected(payload, expected_role, detected):
    with pytest.raises(InnproValidationError, match=detected):
        parse_innpro_iof(payload, expected_role=expected_role)


def test_ambiguous_iof_is_rejected_even_with_expected_role():
    ambiguous = b"""<offer file_format="IOF"><products>
      <product id="1"><mystery/><sizes><size code="A"/></sizes></product>
      </products></offer>"""
    with pytest.raises(InnproValidationError, match="ambiguo"):
        parse_innpro_iof(ambiguous, expected_role="light")


class _ReadSpy(io.BytesIO):
    def __init__(self, value: bytes):
        super().__init__(value)
        self.requests: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.requests.append(size)
        return super().read(size)


def test_seekable_file_object_is_streamed_and_position_is_restored():
    stream = _ReadSpy(LIGHT)
    stream.seek(7)

    result = parse_innpro_iof(stream)

    assert result.feed_role == "light"
    assert stream.tell() == 7
    assert stream.requests
    assert -1 not in stream.requests
    assert max(stream.requests) <= 64 * 1024


@pytest.mark.parametrize(
    "payload",
    [
        b"<products><product/></products>",
        b'<offer file_format="IOF"><products><product></products></offer>',
        b"""<!DOCTYPE offer [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
        <offer file_format="IOF"><products><product id="1">
        <description><name>&xxe;</name></description><sizes><size code="1"/>
        </sizes></product></products></offer>""",
    ],
)
def test_non_iof_malformed_and_xxe_inputs_are_rejected(payload):
    with pytest.raises(InnproValidationError):
        parse_innpro_iof(payload)


def test_byte_product_element_and_depth_limits_are_enforced():
    with pytest.raises(InnproLimitError, match="limite"):
        parse_innpro_iof(LIGHT, maximum_bytes=len(LIGHT) - 1)
    start = LIGHT.index(b"  <product")
    end = LIGHT.index(b"  </product>") + len(b"  </product>")
    product = LIGHT[start:end]
    two_products = LIGHT.replace(b" </products>", product + b"\n </products>")
    with pytest.raises(InnproLimitError, match="prodotti"):
        parse_innpro_iof(two_products, maximum_products=1)

    with pytest.raises(InnproLimitError, match="elementi"):
        parse_innpro_iof(LIGHT, maximum_elements=3)
    with pytest.raises(InnproLimitError, match="profondit"):
        parse_innpro_iof(LIGHT, maximum_depth=3)
    with pytest.raises(InnproLimitError, match="prodotto"):
        parse_innpro_iof(LIGHT, maximum_product_elements=2)


def test_invalid_expected_role_and_empty_products_are_rejected():
    with pytest.raises(InnproValidationError, match="FULL o LIGHT"):
        parse_innpro_iof(LIGHT, expected_role="other")  # type: ignore[arg-type]
    with pytest.raises(InnproValidationError, match="prodotti leggibili"):
        parse_innpro_iof(b'<offer file_format="IOF"><products/></offer>')


def test_dense_small_feed_adaptively_spools_rows_and_releases_temporary_storage():
    products = b"".join(
        (
            f'<product id="{index}"><price net="1.25"/><sizes>'
            f'<size code_producer="590{index:010d}"><stock quantity="2"/>'
            f"</size></sizes></product>"
        ).encode()
        for index in range(1, 1_003)
    )
    payload = (
        b'<offer file_format="IOF"><products currency="EUR">'
        + products
        + b"</products></offer>"
    )
    assert len(payload) < 8 * 1024 * 1024

    result = parse_innpro_iof(payload, expected_role="light")

    assert result.rows_spooled_to_disk is True
    assert len(result.rows) == 1_002
    assert result.rows[0]["source_row"] == 2
    assert result.rows[-1]["ean"] == "5900000001002"
    result.close()
    with pytest.raises(ValueError, match="state chiuse"):
        _ = result.rows[0]


def test_full_disk_spool_preserves_and_replays_complete_product_content(monkeypatch):
    monkeypatch.setattr(innpro, "INNPRO_ROW_SPOOL_THRESHOLD_BYTES", 0)

    with parse_innpro_iof(FULL, expected_role="full") as result:
        assert result.rows_spooled_to_disk is True
        first = result.rows[0]
        replayed = list(result.rows)[0]

        assert replayed == first
        source = json.loads(first["canonical_json"])["source"]
        assert source["description_i18n"]["pl"] == "Polski opis"
        assert source["image_urls"][0] == "https://img.example/main.jpg"
        assert source["attachment_metadata"][0]["name_i18n"]["de"] == "Handbuch"
        assert source["parameters"] == {
            "Color": ["Black", "Red"],
            "Range": "0-10000 ppm",
        }


def test_variant_amplification_is_rejected_before_unbounded_canonicalization():
    sizes = b"".join(
        f'<size code_producer="590{index:010d}"/>'.encode()
        for index in range(501)
    )
    payload = (
        b'<offer file_format="IOF"><products><product id="1"><description>'
        b'<name xml:lang="eng">Bomb guard</name></description><sizes>'
        + sizes
        + b"</sizes></product></products></offer>"
    )

    with pytest.raises(InnproLimitError, match="troppe varianti"):
        parse_innpro_iof(payload, expected_role="full")


def test_normalized_byte_budget_fails_closed_and_closes_disk_spool(monkeypatch):
    opened = []
    real_temporary_file = innpro.tempfile.TemporaryFile

    def tracked_temporary_file(*args, **kwargs):
        stream = real_temporary_file(*args, **kwargs)
        opened.append(stream)
        return stream

    monkeypatch.setattr(innpro.tempfile, "TemporaryFile", tracked_temporary_file)
    monkeypatch.setattr(innpro, "INNPRO_ROW_SPOOL_THRESHOLD_BYTES", 0)
    monkeypatch.setattr(innpro, "MAX_INNPRO_NORMALIZED_BYTES", 8_000)
    description = b"x" * 3_000
    payload = (
        b'<offer file_format="IOF"><products><product id="1"><description>'
        b'<name xml:lang="eng">Budget guard</name><long_desc xml:lang="eng">'
        + description
        + b'</long_desc></description><sizes><size code_producer="5900000000001"/>'
        b"</sizes></product></products></offer>"
    )

    with pytest.raises(InnproLimitError, match="dati prodotto normalizzati"):
        parse_innpro_iof(payload, expected_role="full")

    assert opened
    assert all(stream.closed for stream in opened)


def test_large_unrelated_xml_subtree_is_released_while_streaming(monkeypatch):
    monkeypatch.setattr(innpro, "INNPRO_ROW_SPOOL_THRESHOLD_BYTES", 0)
    payload = (
        b'<offer file_format="IOF"><junk>'
        + (b'<x a="1234567890"/>' * 200_000)
        + b'</junk><products currency="EUR"><product id="1">'
        b'<price net="1.25"/><sizes><size code_producer="5900000000001">'
        b'<stock quantity="2"/></size></sizes></product></products></offer>'
    )

    tracemalloc.start()
    try:
        with parse_innpro_iof(
            payload,
            expected_role="light",
            maximum_elements=200_020,
        ) as result:
            rows = result.rows
            assert result.rows_spooled_to_disk is True
            assert len(rows) == 1
            assert rows[0]["ean"] == "5900000000001"
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak_bytes < 16 * 1024 * 1024
    with pytest.raises(ValueError, match="state chiuse"):
        _ = rows[0]
