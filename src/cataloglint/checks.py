import re
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from xml.etree.ElementTree import Element, ParseError

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

MAX_BYTES = 32 * 1024 * 1024
MAX_ELEMENTS = 300_000
NUMBER = re.compile(r"-?[0-9]{1,18}(?:\.[0-9]{1,6})?")


class InputError(ValueError):
    pass


@dataclass(frozen=True)
class Issue:
    code: str
    severity: str
    source: str
    location: str
    message: str


@dataclass
class Report:
    products: int = 0
    offers: int = 0
    issues: list[Issue] = field(default_factory=list)
    omitted: int = 0

    def add(self, code: str, source: str, location: str, message: str, warning: bool = False):
        if len(self.issues) >= 1000:
            self.omitted += 1
            return
        self.issues.append(
            Issue(code, "warning" if warning else "error", source, location, message)
        )

    def failed(self, strict: bool = False) -> bool:
        return bool(self.omitted or any(i.severity == "error" or strict for i in self.issues))

    def as_dict(self) -> dict:
        return {
            "schema_version": 1,
            "products": self.products,
            "offers": self.offers,
            "omitted_issues": self.omitted,
            "issues": [asdict(i) for i in self.issues],
        }


def read_xml(path: Path) -> Element:
    if not path.is_file():
        raise InputError("Input must be a regular file")
    with path.open("rb") as stream:
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise InputError("XML exceeds 32 MiB")
    try:
        # An iterative parser bounds nesting/element count before building a large tree.
        import io

        stack = []
        root = None
        count = 0
        for event, element in ElementTree.iterparse(
            io.BytesIO(data),
            events=("start", "end"),
            forbid_dtd=True,
            forbid_entities=True,
            forbid_external=True,
        ):
            if event == "start":
                count += 1
                stack.append(element)
                if count > MAX_ELEMENTS or len(stack) > 64:
                    raise InputError("XML exceeds element count or nesting limit")
                if root is None:
                    root = element
                # Accept both namespaced and unqualified CommerceML exports.
                element.tag = element.tag.rsplit("}", 1)[-1]
            else:
                stack.pop()
    except (ParseError, DefusedXmlException) as error:
        raise InputError("Malformed or unsafe XML (DTD and entities are forbidden)") from error
    if root is None or root.tag != "КоммерческаяИнформация":
        raise InputError("Expected КоммерческаяИнформация root")
    if not re.fullmatch(r"2\.[0-9]{1,2}", root.get("ВерсияСхемы", "")):
        raise InputError("Only CommerceML 2.x export structure is supported")
    return root


def one_container(root: Element, tag: str) -> Element:
    elements = root.findall(tag)
    if len(elements) != 1:
        raise InputError(f"Expected exactly one top-level {tag}")
    element = elements[0]
    incremental = element.get("СодержитТолькоИзменения", "false").lower()
    if incremental not in {"false", "0"}:
        raise InputError(f"{tag}: only full exports are supported, not incremental updates")
    return element


def text(element: Element, tag: str) -> str:
    values = element.findall(tag)
    if len(values) > 1:
        raise InputError(f"Repeated scalar field: {tag}")
    return (values[0].text or "").strip() if values else ""


def number(value: str) -> Decimal:
    if not NUMBER.fullmatch(value):
        raise ValueError("Expected a decimal with a dot, up to 18 integer and 6 fractional digits")
    return Decimal(value)


def is_deleted(element: Element) -> bool:
    raw = text(element, "ПометкаУдаления").lower()
    if raw not in {"", "true", "false", "1", "0"}:
        raise InputError("Invalid ПометкаУдаления boolean")
    return raw in {"true", "1"}


def index_products(catalog: Element, source: str, report: Report) -> dict[str, Element]:
    result = {}
    for index, product in enumerate(catalog.findall("./Товары/Товар"), 1):
        location = f"Каталог/Товары/Товар[{index}]"
        identifier = text(product, "Ид")
        if not identifier:
            report.add("MISSING_PRODUCT_ID", source, location, "Product has no identifier")
            continue
        if identifier in result:
            report.add("DUPLICATE_PRODUCT", source, location, "Product identifier already present")
            continue
        result[identifier] = product
        if not is_deleted(product) and not text(product, "Наименование"):
            report.add("MISSING_NAME", source, location, "Active product has no name")
    if not result:
        report.add("EMPTY_CATALOG", source, "Каталог", "No identifiable products in full export")
    return result


def validate(
    catalog_root: Element,
    offers_root: Element,
    catalog_source: str = "catalog",
    offers_source: str = "offers",
    previous_root: Element | None = None,
    max_removed_percent: int = 20,
    variant_separator: str = "#",
) -> Report:
    if not 0 <= max_removed_percent <= 100:
        raise ValueError("Removal threshold must be between 0 and 100")
    if len(variant_separator) > 1:
        raise ValueError("Variant separator must be empty or one character")
    report = Report()
    catalog = one_container(catalog_root, "Каталог")
    package = one_container(offers_root, "ПакетПредложений")
    catalog_id = text(catalog, "Ид")
    if not catalog_id or text(package, "ИдКаталога") != catalog_id:
        report.add(
            "CATALOG_MISMATCH",
            offers_source,
            "ПакетПредложений/ИдКаталога",
            "Offer package does not reference this catalog",
        )
    products = index_products(catalog, catalog_source, report)
    report.products = len(products)
    price_types = {}
    for index, price_type in enumerate(package.findall("./ТипыЦен/ТипЦены"), 1):
        identifier = text(price_type, "Ид")
        if not identifier or identifier in price_types:
            report.add(
                "INVALID_PRICE_TYPE",
                offers_source,
                f"ТипыЦен/ТипЦены[{index}]",
                "Price type identifier is absent or duplicated",
            )
        else:
            price_types[identifier] = text(price_type, "Валюта")

    seen_offers = set()
    for index, offer in enumerate(package.findall("./Предложения/Предложение"), 1):
        report.offers += 1
        location = f"ПакетПредложений/Предложения/Предложение[{index}]"
        identifier = text(offer, "Ид")
        if not identifier:
            report.add("MISSING_OFFER_ID", offers_source, location, "Offer has no identifier")
        elif identifier in seen_offers:
            report.add(
                "DUPLICATE_OFFER", offers_source, location, "Offer identifier already present"
            )
        seen_offers.add(identifier)
        if is_deleted(offer):
            continue
        product_id = identifier
        if product_id not in products and variant_separator and variant_separator in product_id:
            product_id, variant = product_id.split(variant_separator, 1)
            if not variant:
                product_id = ""
        product = products.get(product_id)
        if product is None or is_deleted(product):
            report.add(
                "ORPHAN_OFFER",
                offers_source,
                location,
                "Active offer has no active product in the supplied full catalog",
            )

        quantity = text(offer, "Количество")
        if quantity:
            _check_number(quantity, "Количество", offers_source, location, report, stock=True)
        stocks = offer.findall("Склад")
        seen_stocks = set()
        for stock in stocks:
            warehouse = stock.get("ИдСклада", "")
            if not warehouse or warehouse in seen_stocks:
                report.add(
                    "INVALID_WAREHOUSE",
                    offers_source,
                    location,
                    "Warehouse identifier is absent or repeated",
                )
            seen_stocks.add(warehouse)
            _check_number(
                stock.get("КоличествоНаСкладе", ""),
                "КоличествоНаСкладе",
                offers_source,
                location,
                report,
                stock=True,
            )
        if not quantity and not stocks:
            report.add(
                "MISSING_STOCK",
                offers_source,
                location,
                "No total or per-warehouse stock supplied",
                True,
            )

        prices = offer.findall("./Цены/Цена")
        if not prices:
            report.add("MISSING_PRICE", offers_source, location, "Active offer has no prices")
        seen_prices = set()
        for price in prices:
            price_type = text(price, "ИдТипаЦены")
            minimum = text(price, "МинКоличество") or "0"
            minimum_value = _check_number(minimum, "МинКоличество", offers_source, location, report)
            key = (
                price_type,
                minimum_value,
                text(price, "Единица"),
                text(price, "Валюта") or price_types.get(price_type),
            )
            if key in seen_prices:
                report.add(
                    "DUPLICATE_PRICE",
                    offers_source,
                    location,
                    "Price type, unit, currency and quantity tier repeated",
                )
            seen_prices.add(key)
            if price_type not in price_types:
                report.add(
                    "UNKNOWN_PRICE_TYPE",
                    offers_source,
                    location,
                    "Price type is not declared in the package",
                )
            amount = _check_number(
                text(price, "ЦенаЗаЕдиницу"), "ЦенаЗаЕдиницу", offers_source, location, report
            )
            if amount == 0:
                report.add(
                    "ZERO_PRICE",
                    offers_source,
                    location,
                    "Zero price; confirm this is intentional",
                    True,
                )
            currency = text(price, "Валюта") or price_types.get(price_type)
            if not currency:
                report.add(
                    "MISSING_CURRENCY",
                    offers_source,
                    location,
                    "Neither price nor price type defines a currency",
                )
            coefficient = text(price, "Коэффициент")
            if coefficient:
                value = _check_number(coefficient, "Коэффициент", offers_source, location, report)
                if value == 0:
                    report.add(
                        "ZERO_COEFFICIENT",
                        offers_source,
                        location,
                        "Unit coefficient must be positive",
                    )
    if not report.offers:
        report.add(
            "EMPTY_OFFERS", offers_source, "ПакетПредложений", "Full export contains no offers"
        )
    if previous_root is not None:
        previous_catalog = one_container(previous_root, "Каталог")
        if text(previous_catalog, "Ид") != catalog_id:
            raise InputError("Previous export belongs to a different catalog")
        previous_report = Report()
        previous = index_products(previous_catalog, "previous", previous_report)
        if previous_report.failed():
            raise InputError("Previous catalog contains invalid or duplicate products")
        before = {identifier for identifier, product in previous.items() if not is_deleted(product)}
        after = {identifier for identifier, product in products.items() if not is_deleted(product)}
        removed = len(before - after)
        if before and removed * 100 > len(before) * max_removed_percent:
            report.add(
                "CATALOG_SHRINK",
                catalog_source,
                "Каталог",
                f"{removed} of {len(before)} previously active products disappeared or became deleted; threshold {max_removed_percent}%",
            )
    return report


def _check_number(
    value: str, field: str, source: str, location: str, report: Report, stock: bool = False
) -> Decimal | None:
    try:
        parsed = number(value)
    except ValueError as error:
        report.add("INVALID_NUMBER", source, location + "/" + field, str(error))
        return None
    if parsed < 0:
        report.add(
            "NEGATIVE_STOCK" if stock else "NEGATIVE_NUMBER",
            source,
            location + "/" + field,
            "Negative stock; review backorder policy" if stock else "Value must not be negative",
            stock,
        )
    return parsed
