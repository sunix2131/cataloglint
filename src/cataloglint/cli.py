import argparse
import io
import json
import sys
from pathlib import Path

from .checks import InputError, Report, read_xml, validate


def percent(value: str) -> int:
    try:
        result = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("Expected an integer percentage") from error
    if not 0 <= result <= 100:
        raise argparse.ArgumentTypeError("Percentage must be between 0 and 100")
    return result


def main(argv: list[str] | None = None) -> int:
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(errors="backslashreplace")
    parser = argparse.ArgumentParser(
        description="Check a full CommerceML catalog and its offers offline."
    )
    parser.add_argument("catalog", type=Path, help="Full import.xml")
    parser.add_argument("offers", type=Path, help="Full offers.xml; may be the same combined file")
    parser.add_argument(
        "--previous-catalog", type=Path, help="Previous full catalog for deletion guard"
    )
    parser.add_argument("--max-removed-percent", type=percent, default=20)
    parser.add_argument(
        "--variant-separator",
        choices=["#", ""],
        default="#",
        help="Product/variant separator; empty disables variant fallback",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--strict", action="store_true", help="Fail on warnings, including zero prices"
    )
    args = parser.parse_args(argv)
    try:
        catalog = read_xml(args.catalog)
        offers = (
            catalog if args.catalog.resolve() == args.offers.resolve() else read_xml(args.offers)
        )
        previous = read_xml(args.previous_catalog) if args.previous_catalog else None
        report = validate(
            catalog,
            offers,
            args.catalog.name,
            args.offers.name,
            previous,
            args.max_removed_percent,
            args.variant_separator,
        )
        code = int(report.failed(args.strict))
    except (InputError, OSError) as error:
        report = Report()
        message = (
            str(error) if isinstance(error, InputError) else "Cannot read one of the input files"
        )
        report.add("INPUT_ERROR", "input", "", message)
        code = 2
    payload = report.as_dict()
    payload.update(ok=code == 0, exit_code=code)
    if args.json:
        print(json.dumps(payload, ensure_ascii=True, indent=2))
    else:
        print(f"{report.products} products, {report.offers} offers; {len(report.issues)} findings")
        for issue in report.issues:
            source = json.dumps(issue.source, ensure_ascii=True)
            print(
                f"{issue.severity.upper()} {issue.code} {source} {issue.location}: {issue.message}"
            )
        if report.omitted:
            print(f"{report.omitted} further findings omitted; check fails closed.")
    return code
