"""
Spreadsheet-safe CSV for every register export.

A cell that starts with = + - @ (or a tab, carriage return or line feed), even after leading spaces,
is run as a formula by Excel, LibreOffice and Google Sheets. Anyone who can type into a record (a
subcontractor's company name, an RFI subject) could otherwise plant one in our users' spreadsheets.
`safe()` puts a ' in front of such text. Plain numbers (-120.00, -1.5%), numbers (Decimal, int)
and dates are data, not formulas, and go out unchanged. Anything else (a User, a Subcontractor) is
someone's typing once it is text, so it is checked like any string.

    from core.exports import csv_response
    return csv_response('LDS-001-rfis.csv', ['Ref', 'Subject'], ((r.reference, r.title) for r in rfis))

Or `w = core.exports.writer(resp)` in place of csv.writer, or pass each cell through `safe()`.
"""
import csv
import datetime
import re
from decimal import Decimal

from django.http import HttpResponse

FORMULA_STARTS = ('=', '+', '-', '@', '\t', '\r', '\n')
NUMBER = re.compile(r'-?[0-9]+(\.[0-9]+)?%?')  # ASCII digits only


def safe(value):
    """Text a spreadsheet won't run as a formula, even after leading spaces. Money goes out as plain 2dp text."""
    if value is None:
        return ''
    if isinstance(value, (bool, int, float, Decimal, datetime.date, datetime.time)):
        return str(value)
    if not isinstance(value, str):
        value = str(value)  # a model instance's text is what someone typed (a name): check it like any string
    if NUMBER.fullmatch(value):  # a plain number such as -120.00 or -1.5% is data, not a formula
        return value
    if value[:1] in FORMULA_STARTS or value.lstrip()[:1] in FORMULA_STARTS:
        return "'" + value
    return value


def safe_row(values):
    return [safe(v) for v in values]


class SafeWriter:
    """csv.writer that passes every cell through safe(): `w = writer(resp)` then `w.writerow([...])`."""

    def __init__(self, f, **kwargs):
        self._w = csv.writer(f, **kwargs)

    def writerow(self, row):
        return self._w.writerow(safe_row(row))

    def writerows(self, rows):
        for row in rows:
            self.writerow(row)


def writer(f, **kwargs):
    return SafeWriter(f, **kwargs)


def csv_response(filename, header, rows):
    """A CSV download with every cell, the header included, passed through safe()."""
    resp = HttpResponse(content_type='text/csv')
    resp['Content-Disposition'] = f'attachment; filename="{filename}"'
    w = writer(resp)
    w.writerow(header)
    w.writerows(rows)
    return resp
