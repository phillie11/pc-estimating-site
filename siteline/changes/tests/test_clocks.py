"""
The advisory NEC4 clocks and the JCT notice-of-delay prompt, held to fixed tables. Pure
functions, so plain SimpleTestCase: no database.
"""
from datetime import date
from types import SimpleNamespace

from django.test import SimpleTestCase

from changes import clocks as c
from contracts.vocabulary import vocabulary

D = date.fromisoformat


class CalendarWeekTests(SimpleTestCase):
    # (start, weeks, expected): NEC4 clause 13 counts calendar weeks, so bank holidays, weekends,
    # Christmas, Easter and 29 February are all ordinary days
    TABLE = [
        ('2026-01-05', 8, '2026-03-02'),   # plain Monday to Monday
        ('2026-12-21', 8, '2027-02-15'),   # across Christmas, Boxing Day, the 28 Dec substitute and New Year
        ('2026-04-01', 3, '2026-04-22'),   # across Good Friday (3 Apr) and Easter Monday (6 Apr)
        ('2026-04-22', 2, '2026-05-06'),   # across the early May bank holiday (4 May)
        ('2028-01-10', 8, '2028-03-06'),   # leap year: 29 Feb 2028 counts
        ('2027-01-10', 8, '2027-03-07'),   # no leap day
        ('2026-08-29', 2, '2026-09-12'),   # Saturday start: no roll to a working day
        ('2026-12-31', 3, '2027-01-21'),   # year end
    ]

    def test_table(self):
        for start, weeks, expected in self.TABLE:
            with self.subTest(start=start, weeks=weeks):
                self.assertEqual(c.weeks_after(D(start), weeks), D(expected))

    def test_deadlines_use_the_contract_periods(self):
        self.assertEqual(c.notification_deadline(D('2026-12-21')), D('2027-02-15'))
        self.assertEqual(c.quotation_deadline(D('2026-04-01')), D('2026-04-22'))
        self.assertEqual(c.reply_deadline(D('2026-04-22')), D('2026-05-06'))


class TimeBarTests(SimpleTestCase):
    AWARE = D('2026-01-05')  # deadline 2 Mar 2026

    # (today, notified_on, tone, days_left, phrase in message)
    TABLE = [
        ('2026-01-05', None, c.TONE_OK, 56, '56 days left'),
        ('2026-02-22', None, c.TONE_OK, 8, '8 days left'),
        ('2026-02-23', None, c.TONE_SOON, 7, '7 days left'),
        ('2026-03-01', None, c.TONE_SOON, 1, '1 day left'),
        ('2026-03-02', None, c.TONE_SOON, 0, 'Due today'),
        ('2026-03-03', None, c.TONE_LATE, -1, '1 day overdue'),
        ('2026-03-12', None, c.TONE_LATE, -10, '10 days overdue'),
        ('2026-03-12', '2026-03-02', c.TONE_DONE, None, 'Notified in time'),   # on the last day: in time
        ('2026-03-12', '2026-03-03', c.TONE_LATE, None, '1 day after the deadline'),
        ('2026-03-12', '2026-01-05', c.TONE_DONE, None, 'Notified in time'),  # same day as aware
    ]

    def test_table(self):
        for today, notified, tone, left, phrase in self.TABLE:
            with self.subTest(today=today, notified=notified):
                [clock] = c.nec_clocks(D(today), aware_on=self.AWARE, notified_on=notified and D(notified))
                self.assertEqual(clock.key, 'notify')
                self.assertEqual(clock.deadline, D('2026-03-02'))
                self.assertEqual((clock.tone, clock.days_left), (tone, left))
                self.assertIn(phrase, clock.message)
                self.assertEqual(clock.clause, 'NEC4 61.3')

    def test_late_message_names_the_time_bar(self):
        [clock] = c.nec_clocks(D('2026-03-03'), aware_on=self.AWARE)
        self.assertIn('8-week time bar', clock.message)
        self.assertTrue(clock.is_late and clock.needs_attention)
        self.assertEqual(clock.days_late, 1)

    def test_vocabulary_names_the_project_manager(self):
        [clock] = c.nec_clocks(D('2026-01-05'), aware_on=self.AWARE, words=vocabulary('NEC4_ECC'))
        self.assertIn('Project Manager', clock.message)


class QuotationAndReplyTests(SimpleTestCase):
    def test_quotation_clock(self):
        # instructed 1 Apr, due 22 Apr
        for today, submitted, tone, left in [('2026-04-15', None, c.TONE_SOON, 7), ('2026-04-14', None, c.TONE_OK, 8),
                                             ('2026-04-23', None, c.TONE_LATE, -1),
                                             ('2026-04-30', '2026-04-22', c.TONE_DONE, None),
                                             ('2026-04-30', '2026-04-24', c.TONE_LATE, None)]:
            with self.subTest(today=today, submitted=submitted):
                clocks = c.nec_clocks(D(today), quotation_instructed_on=D('2026-04-01'),
                                      quotation_submitted_on=submitted and D(submitted))
                quote = clocks[0]
                self.assertEqual((quote.key, quote.deadline), ('quote', D('2026-04-22')))
                self.assertEqual((quote.tone, quote.days_left), (tone, left))

    def test_reply_clock_follows_submission(self):
        clocks = c.nec_clocks(D('2026-05-07'), quotation_submitted_on=D('2026-04-22'))
        [reply] = clocks
        self.assertEqual((reply.key, reply.deadline, reply.tone, reply.days_left), ('reply', D('2026-05-06'), c.TONE_LATE, -1))
        self.assertIn('treated as accepted', reply.message)

    def test_reply_in_time_is_done(self):
        [reply] = c.nec_clocks(D('2026-06-01'), quotation_submitted_on=D('2026-04-22'), reply_received_on=D('2026-05-06'))
        self.assertEqual(reply.tone, c.TONE_DONE)


class DeemedAcceptanceTests(SimpleTestCase):
    SUBMITTED = D('2026-04-22')  # reply due 6 May

    # (failure notified, reply received, deemed date or None)
    TABLE = [
        (None, None, None),                          # no notice of failure: nothing deemed
        ('2026-05-06', None, None),                  # notice on the reply's last day: too early
        ('2026-05-01', None, None),                  # notice before the reply was due: doesn't count
        ('2026-05-07', None, '2026-05-21'),          # first day late: deemed two weeks on
        ('2026-05-25', None, '2026-06-08'),          # late notice, across the 25 May bank holiday
        ('2026-05-07', '2026-05-15', None),          # PM replied before deemed acceptance
        ('2026-05-07', '2026-05-21', '2026-05-21'),  # reply on the deemed day itself: too late
    ]

    def test_table(self):
        for failure, reply, deemed in self.TABLE:
            with self.subTest(failure=failure, reply=reply):
                self.assertEqual(c.deemed_acceptance_date(self.SUBMITTED, failure and D(failure), reply and D(reply)),
                                 deemed and D(deemed))

    def test_clock_counts_down_then_reports_accepted(self):
        clocks = c.nec_clocks(D('2026-05-14'), quotation_submitted_on=self.SUBMITTED,
                              failure_notified_on=D('2026-05-07'))
        deemed = clocks[-1]
        self.assertEqual((deemed.key, deemed.deadline, deemed.days_left, deemed.tone),
                         ('deemed', D('2026-05-21'), 7, c.TONE_SOON))
        self.assertIn('Treated as accepted on 21 May 2026 unless', deemed.message)
        after = c.nec_clocks(D('2026-05-21'), quotation_submitted_on=self.SUBMITTED, failure_notified_on=D('2026-05-07'))
        self.assertEqual(after[-1].tone, c.TONE_DONE)
        self.assertIn('Treated as accepted on 21 May 2026:', after[-1].message)

    def test_early_notice_of_failure_is_flagged(self):
        clocks = c.nec_clocks(D('2026-05-10'), quotation_submitted_on=self.SUBMITTED, failure_notified_on=D('2026-05-05'))
        self.assertEqual(clocks[-1].key, 'deemed')
        self.assertIn('Notify again after 6 May 2026', clocks[-1].message)


class JCTPromptTests(SimpleTestCase):
    def test_prompt_only_for_time_impacting_events(self):
        words = vocabulary('JCT_IC')
        self.assertIsNone(c.jct_delay_prompt(None, None, words))
        self.assertIsNone(c.jct_delay_prompt(0, None, words))
        prompt = c.jct_delay_prompt(4, None, words)
        self.assertEqual((prompt.key, prompt.tone), ('delay_notice', c.TONE_SOON))
        self.assertIn('Contract Administrator', prompt.message)
        self.assertIn('Relevant Event', prompt.message)
        done = c.jct_delay_prompt(4, D('2026-03-02'), words)
        self.assertEqual(done.tone, c.TONE_DONE)
        self.assertIn('2 Mar 2026', done.message)

    def test_design_and_build_names_the_employers_agent(self):
        self.assertIn("Employer's Agent", c.jct_delay_prompt(2, None, vocabulary('JCT_DB')).message)


class ClocksForTests(SimpleTestCase):
    def ce(self, **kw):
        base = dict(status='IDENTIFIED', aware_on=None, notified_on=None, quotation_instructed_on=None,
                    quotation_submitted_on=None, reply_received_on=None, failure_notified_on=None,
                    time_impact_days=None, delay_notice_given_on=None)
        return SimpleNamespace(**{**base, **kw})

    def contract(self, form):
        from contracts.models import MainContract
        return MainContract(form=form)

    def test_by_family(self):
        ce = self.ce(aware_on=D('2026-01-05'), time_impact_days=3)
        self.assertEqual([k.key for k in c.clocks_for(ce, self.contract('NEC4_ECC'), D('2026-01-10'))], ['notify'])
        self.assertEqual([k.key for k in c.clocks_for(ce, self.contract('JCT_SBC'), D('2026-01-10'))], ['delay_notice'])
        self.assertEqual(c.clocks_for(ce, self.contract('BESPOKE'), D('2026-01-10')), [])
        self.assertEqual(c.clocks_for(ce, None, D('2026-01-10')), [])

    def test_closed_events_keep_only_what_was_done(self):
        ce = self.ce(status='AGREED', aware_on=D('2026-01-05'), notified_on=D('2026-01-06'),
                     quotation_instructed_on=D('2026-01-07'))
        clocks = c.clocks_for(ce, self.contract('NEC4_ECC'), D('2026-06-01'))
        self.assertEqual([k.key for k in clocks], ['notify'])
