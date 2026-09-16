import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from xml.etree.ElementTree import SubElement, tostring

import pytest

from cataloglint.checks import InputError, Report, number, read_xml, validate
from cataloglint.cli import main, percent

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def roots():
    return read_xml(EXAMPLES / "import.xml"), read_xml(EXAMPLES / "offers.xml")


def findings(offer_change=None, catalog_change=None, **kwargs):
    catalog, offers = roots()
    if offer_change:
        offer_change(offers)
    if catalog_change:
        catalog_change(catalog)
    return validate(catalog, offers, **kwargs)


def offer(root):
    return root.find("./ПакетПредложений/Предложения/Предложение")


def price(root):
    return offer(root).find("./Цены/Цена")


def set_text(root, path, value):
    element = root.find(path)
    assert element is not None
    element.text = value


def codes(report):
    return {issue.code for issue in report.issues}


def test_valid_full_export_and_variant():
    report = findings()
    assert (report.products, report.offers) == (2, 2)
    assert report.issues == []


def test_namespaces_are_supported(tmp_path):
    path = tmp_path / "namespaced.xml"
    text = (EXAMPLES / "import.xml").read_text(encoding="utf-8")
    path.write_text(
        text.replace(
            "<КоммерческаяИнформация ", '<КоммерческаяИнформация xmlns="urn:1C.ru:commerceml_2" '
        ),
        encoding="utf-8",
    )
    report = validate(read_xml(path), read_xml(EXAMPLES / "offers.xml"))
    assert not report.failed()


@pytest.mark.parametrize(
    "content",
    [
        "<broken>",
        "<wrong/>",
        '<КоммерческаяИнформация ВерсияСхемы="1.0"/>',
        '<!DOCTYPE x [<!ENTITY local SYSTEM "file:///etc/passwd">]><x>&local;</x>',
        '<!DOCTYPE x [<!ENTITY x "123">]><x>&x;</x>',
        "<!DOCTYPE x><x/>",
        "<x>" * 65 + "</x>" * 65,
    ],
)
def test_rejects_invalid_or_unsafe_xml(tmp_path, content):
    path = tmp_path / "bad.xml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(InputError):
        read_xml(path)


def test_xml_limits_and_nonfiles(tmp_path, monkeypatch):
    with pytest.raises(InputError):
        read_xml(tmp_path)
    path = tmp_path / "bounded.xml"
    path.write_text(
        '<КоммерческаяИнформация ВерсияСхемы="2.10"><a/></КоммерческаяИнформация>', encoding="utf-8"
    )
    monkeypatch.setattr("cataloglint.checks.MAX_ELEMENTS", 1)
    with pytest.raises(InputError, match="element count"):
        read_xml(path)
    monkeypatch.setattr("cataloglint.checks.MAX_BYTES", 2)
    with pytest.raises(InputError, match="32 MiB"):
        read_xml(path)


@pytest.mark.parametrize("value", ["true", "1", "yes"])
def test_incremental_exports_are_explicitly_rejected(value):
    with pytest.raises(InputError, match="full exports"):
        findings(
            catalog_change=lambda root: root.find("Каталог").set("СодержитТолькоИзменения", value)
        )


def test_missing_or_ambiguous_container():
    catalog, offers = roots()
    catalog.append(deepcopy(catalog.find("Каталог")))
    with pytest.raises(InputError, match="exactly one"):
        validate(catalog, offers)
    with pytest.raises(InputError):
        validate(offers, offers)


def test_repeated_scalar_fields_rejected():
    def modify(root):
        SubElement(offer(root), "Ид").text = "second"

    with pytest.raises(InputError, match="Repeated scalar"):
        findings(modify)


def test_duplicate_products_and_offers():
    def products(root):
        container = root.find("./Каталог/Товары")
        container.append(deepcopy(container[0]))

    def offers(root):
        container = root.find("./ПакетПредложений/Предложения")
        container.append(deepcopy(container[0]))

    report = findings(offers, products)
    assert {"DUPLICATE_PRODUCT", "DUPLICATE_OFFER"} <= codes(report)


def test_missing_ids_and_names():
    def products(root):
        root.find("./Каталог/Товары/Товар/Ид").text = ""
        root.find("./Каталог/Товары")[1].find("Наименование").text = ""

    report = findings(lambda root: setattr(offer(root).find("Ид"), "text", ""), products)
    assert {"MISSING_PRODUCT_ID", "MISSING_NAME", "MISSING_OFFER_ID"} <= codes(report)


def test_orphan_deleted_product_and_variant_optout():
    assert "ORPHAN_OFFER" in codes(findings(variant_separator=""))
    report = findings(
        catalog_change=lambda root: setattr(
            SubElement(root.find("./Каталог/Товары/Товар"), "ПометкаУдаления"), "text", "true"
        )
    )
    assert "ORPHAN_OFFER" in codes(report)
    report = findings(lambda root: setattr(offer(root).find("Ид"), "text", "coffee#"))
    assert "ORPHAN_OFFER" in codes(report)


def test_deleted_offer_needs_no_price_or_stock():
    def modify(root):
        item = offer(root)
        item.remove(item.find("Цены"))
        item.remove(item.find("Количество"))
        item.find("Ид").text = "removed-id"
        SubElement(item, "ПометкаУдаления").text = "true"

    assert findings(modify).issues == []


def test_invalid_deletion_boolean():
    with pytest.raises(InputError, match="boolean"):
        findings(lambda root: setattr(SubElement(offer(root), "ПометкаУдаления"), "text", "maybe"))


@pytest.mark.parametrize(
    "value,expected",
    [
        ("-1", "NEGATIVE_NUMBER"),
        ("NaN", "INVALID_NUMBER"),
        ("Infinity", "INVALID_NUMBER"),
        ("1e9", "INVALID_NUMBER"),
        ("1,23", "INVALID_NUMBER"),
        ("0.0000001", "INVALID_NUMBER"),
        ("9" * 100, "INVALID_NUMBER"),
        ("", "INVALID_NUMBER"),
        ("0", "ZERO_PRICE"),
    ],
)
def test_price_numbers(value, expected):
    report = findings(lambda root: setattr(price(root).find("ЦенаЗаЕдиницу"), "text", value))
    assert expected in codes(report)


def test_zero_price_and_negative_stock_are_reviewable_warnings():
    def modify(root):
        price(root).find("ЦенаЗаЕдиницу").text = "0"
        offer(root).find("Количество").text = "-1"

    report = findings(modify)
    assert not report.failed()
    assert report.failed(strict=True)
    assert {"ZERO_PRICE", "NEGATIVE_STOCK"} <= codes(report)


def test_exact_decimal_values():
    assert str(number("999999999999999999.999999")) == "999999999999999999.999999"


def test_catalog_identity_and_empty_exports():
    def modify(root):
        root.find("./ПакетПредложений/ИдКаталога").text = "other"
        root.find("./ПакетПредложений/Предложения").clear()

    report = findings(modify, lambda root: root.find("./Каталог/Товары").clear())
    assert {"CATALOG_MISMATCH", "EMPTY_CATALOG", "EMPTY_OFFERS"} <= codes(report)


def test_price_type_and_currency_checks():
    def modify(root):
        types = root.find("./ПакетПредложений/ТипыЦен")
        types.append(deepcopy(types[0]))
        price(root).find("Валюта").text = "EUR"

    report = findings(modify)
    assert "INVALID_PRICE_TYPE" in codes(report)
    assert not findings(lambda root: setattr(price(root).find("Валюта"), "text", "EUR")).failed()

    def missing(root):
        price(root).find("ИдТипаЦены").text = "missing"
        price(root).remove(price(root).find("Валюта"))

    assert {"UNKNOWN_PRICE_TYPE", "MISSING_CURRENCY"} <= codes(findings(missing))


def test_price_tiers_and_coefficients():
    def modify(root):
        prices = offer(root).find("Цены")
        prices.append(deepcopy(prices[0]))
        price(root).find("Коэффициент").text = "0"

    assert {"DUPLICATE_PRICE", "ZERO_COEFFICIENT"} <= codes(findings(modify))

    def tiers(root):
        prices = offer(root).find("Цены")
        bulk = deepcopy(prices[0])
        SubElement(bulk, "МинКоличество").text = "10"
        prices.append(bulk)

    assert findings(tiers).issues == []


def test_missing_prices_stock_and_bad_warehouse():
    def modify(root):
        offer(root).remove(offer(root).find("Цены"))
        offer(root).remove(offer(root).find("Количество"))
        second = root.find("./ПакетПредложений/Предложения")[1]
        second.append(deepcopy(second.find("Склад")))

    assert {"MISSING_PRICE", "MISSING_STOCK", "INVALID_WAREHOUSE"} <= codes(findings(modify))


def test_shrink_guard_and_threshold_boundary():
    catalog, offers = roots()
    previous = deepcopy(catalog)
    catalog.find("./Каталог/Товары").remove(catalog.find("./Каталог/Товары")[1])
    assert "CATALOG_SHRINK" in codes(
        validate(catalog, offers, previous_root=previous, max_removed_percent=49)
    )
    assert "CATALOG_SHRINK" not in codes(
        validate(catalog, offers, previous_root=previous, max_removed_percent=50)
    )


def test_deleted_products_count_as_removed():
    catalog, offers = roots()
    previous = deepcopy(catalog)
    for product in catalog.findall("./Каталог/Товары/Товар"):
        SubElement(product, "ПометкаУдаления").text = "true"
    assert "CATALOG_SHRINK" in codes(validate(catalog, offers, previous_root=previous))


def test_previous_catalog_must_be_consistent():
    catalog, offers = roots()
    previous = deepcopy(catalog)
    previous.find("./Каталог/Ид").text = "other"
    with pytest.raises(InputError, match="different catalog"):
        validate(catalog, offers, previous_root=previous)
    previous.find("./Каталог/Ид").text = "demo-catalog"
    previous.find("./Каталог/Товары").clear()
    with pytest.raises(InputError, match="invalid"):
        validate(catalog, offers, previous_root=previous)


def test_bad_api_options():
    catalog, offers = roots()
    with pytest.raises(ValueError):
        validate(catalog, offers, max_removed_percent=101)
    with pytest.raises(ValueError):
        validate(catalog, offers, variant_separator="##")
    for value in ["no", "-1", "101"]:
        with pytest.raises(Exception, match="percentage|Percentage"):
            percent(value)


def test_report_is_bounded_and_fails_closed():
    report = Report()
    for _ in range(1001):
        report.add("X", "test", "", "test", True)
    assert report.omitted == 1
    assert report.failed()


def test_cli_exit_codes_and_no_source_changes(tmp_path, capsys):
    catalog, offers = EXAMPLES / "import.xml", EXAMPLES / "offers.xml"
    before = (catalog.read_bytes(), offers.read_bytes())
    assert main([str(catalog), str(offers), "--strict", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] and payload["products"] == 2
    assert main([str(catalog), str(EXAMPLES / "broken-offers.xml")]) == 1
    assert "ORPHAN_OFFER" in capsys.readouterr().out
    assert main([str(catalog), str(tmp_path / "missing"), "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["exit_code"] == 2
    assert (catalog.read_bytes(), offers.read_bytes()) == before
    result = subprocess.run([sys.executable, "-m", "cataloglint", "--help"], capture_output=True)
    assert result.returncode == 0


def test_combined_file_and_previous_cli(tmp_path, capsys):
    catalog, offers = roots()
    catalog.append(offers.find("ПакетПредложений"))
    path = tmp_path / "combined.xml"
    path.write_bytes(tostring(catalog, encoding="utf-8"))
    assert main([str(path), str(path), "--previous-catalog", str(EXAMPLES / "import.xml")]) == 0
    assert "2 products, 2 offers" in capsys.readouterr().out
