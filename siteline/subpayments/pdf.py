"""
The payment advice for one payment to a subcontractor, as PDF: our letterhead (payments.pdf), the
application's figures, then gross due, materials, labour, CIS deduction, VAT and net payable.
Every piece of text goes through documents.pdf.markup (via payments.pdf._p), so nothing a person
typed is parsed as ReportLab markup.
"""
import io

from django.utils import timezone
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Spacer

from payments.pdf import _date, _grid, _money, _p, _styles, letterhead

from . import rules


def advice_pdf(payment):
    app = payment.application
    sc = app.subcontract
    sub = sc.package.subcontractor
    org, project = app.organization, app.project
    symbol = org.currency_symbol or '£'
    s = _styles()
    buf = io.BytesIO()
    story = [letterhead(org, s), Spacer(1, 6 * mm), _p('Payment advice', s['Title']),
             _p(f'{app.reference} {app.title}', s['Heading3'])]
    details = [
        [_p('To', s['Normal']), _p(sub.company_name + (f'\n{sub.address}' if sub.address else ''), s['Normal'])],
        [_p('Project', s['Normal']), _p(f'{project.reference} {project.name}', s['Normal'])],
        [_p('Package', s['Normal']), _p(str(sc.package.trade or 'Subcontract'), s['Normal'])],
        [_p('Valuation date', s['Normal']), _p(_date(app.valuation_date), s['Normal'])],
        [_p('Date paid', s['Normal']), _p(_date(payment.paid_on), s['Normal'])],
        [_p('Payment reference', s['Normal']), _p(payment.reference or '-', s['Normal'])],
    ]
    story += [_grid(details, [45 * mm, 141 * mm], s), Spacer(1, 5 * mm)]

    stage = dict(rules.STAGE_CHOICES).get(app.retention_stage, '')
    rows = [
        ('Gross value of work to date', app.gross_to_date),
        ('Approved variations to date', app.variations_value),
        ('Gross valuation', app.gross_valuation),
        (f'Less retention at {app.retention_percent.normalize()}% ({stage.lower()})', -app.retention),
        ('Less previous amounts', -app.previous_certified),
        ('Applied for this time', app.amount_due),
        (f'Sum due ({_basis(app)})', app.notified_sum),
    ]
    if app.discount:
        rows.append((f'Less discount at {app.discount_percent.normalize()}%', -app.discount))
    table = [[_p(label, s['Normal']), _p(_money(v, symbol), s['Right'])] for label, v in rows]
    story += [_p('The application', s['Heading4']), _grid(table, [140 * mm, 46 * mm], s, total_rows=(2, 5, 6)),
              Spacer(1, 4 * mm)]

    pay = [
        ('Gross due this payment (excluding VAT)', _money(payment.gross, symbol)),
        ('Materials', _money(payment.materials, symbol)),
        ('Labour (gross less materials)', _money(payment.labour, symbol)),
        (f'CIS deduction at {payment.cis_rate.normalize()}% of labour', '-' + _money(payment.cis_deduction, symbol)),
        ('VAT', 'Reverse charge' if payment.reverse_charge else _money(payment.vat, symbol)),
        ('Net payable to you', _money(payment.net, symbol)),
    ]
    if not payment.reverse_charge:
        pay[4] = (f'VAT at {payment.vat_rate.normalize()}%', _money(payment.vat, symbol))
    table = [[_p(a, s['Normal']), _p(b, s['RightBold'] if i == len(pay) - 1 else s['Right'])]
             for i, (a, b) in enumerate(pay)]
    story += [_p('This payment', s['Heading4']), _grid(table, [140 * mm, 46 * mm], s, total_rows=(0, len(pay) - 1)),
              Spacer(1, 3 * mm)]
    status = dict(rules.CIS_CHOICES).get(payment.cis_status, payment.cis_status)
    story.append(_p(f'CIS status: {status}' + (f', verification number {payment.verification_number}'
                                                if payment.verification_number else '')
                    + '. The CIS deduction is paid to HMRC on your behalf and counts towards your tax. Materials '
                      'are as you stated them in your application, unless we told you otherwise.', s['Small']))
    if payment.reverse_charge:
        story.append(_p(rules.REVERSE_CHARGE_NOTE, s['Normal']))
    story.append(_p(rules.HMRC_CHECK + ' ' + rules.ADVISORY, s['Small']))
    if payment.is_reversed:
        story += [Spacer(1, 4 * mm), _p(f'This payment was reversed on {_date(timezone.localdate(payment.reversed_at))}: '
                                        f'{payment.reversal_reason}', s['Heading4'])]

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont('Helvetica', 7)
        canvas.drawString(12 * mm, 7 * mm, f'{org.name} · {app.reference} payment advice · printed '
                                           f'{timezone.localtime():%d %b %Y %H:%M}')
        canvas.drawRightString(A4[0] - 12 * mm, 7 * mm, f'Page {doc.page}')
        canvas.restoreState()

    SimpleDocTemplate(buf, pagesize=A4, leftMargin=12 * mm, rightMargin=12 * mm, topMargin=12 * mm,
                      bottomMargin=14 * mm, title=f'Payment advice {app.reference}').build(
        story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()


def _basis(app):
    return {rules_basis: label for rules_basis, label in (
        ('pay_less', 'our pay less notice'), ('payment_notice', 'our payment notice'),
        ('default', 'your application'))}[app.basis]
