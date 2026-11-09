import unittest
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from proteus import Model, Wizard
from trytond.modules.company.tests.tools import create_company, get_company
from trytond.tests.test_tryton import drop_db
from trytond.tests.tools import activate_modules


class TestSalesHistoryFromSales(unittest.TestCase):

    def setUp(self):
        drop_db()
        super().setUp()

    def tearDown(self):
        drop_db()
        super().tearDown()

    def test(self):
        config = activate_modules('sale_forecast')

        History = Model.get('sales.forecast.sale_history')
        Party = Model.get('party.party')
        ProductTemplate = Model.get('product.template')
        ProductUom = Model.get('product.uom')
        Sale = Model.get('sale.sale')
        Forecast = Model.get('sales.forecast')
        StockLocation = Model.get('stock.location')

        create_company()
        company = get_company()
        unit, = ProductUom.find([('name', '=', 'Unit')])
        warehouse, = StockLocation.find([('code', '=', 'WH')])

        party = Party(name='Sales History Customer')
        address = party.addresses.new()
        address.street = 'Main Street'
        address.city = 'Barcelona'
        party.save()

        template = ProductTemplate(
            name='Sales History Product',
            default_uom=unit,
            sale_uom=unit,
            salable=True,
            type='goods',
            cost_price_method='fixed')
        template.sale_forecast_method = 'key_accounts'
        template.sale_forecast_key_account_source = 'orders'
        template.save()

        def create_sale(quantity, when=None):
            sale = Sale(
                party=party,
                invoice_party=party,
                shipment_party=party,
                warehouse=warehouse,
                company=company,
                currency=company.currency,
                sale_date=when or date.today(),
                shipping_date=when or date.today())
            line = sale.lines.new()
            line.product = template.products[0]
            line.quantity = quantity
            line.unit = unit
            line.unit_price = Decimal('1')
            sale.save()
            return sale

        confirmed_sale = create_sale(12)
        confirmed_sale.click('quote')
        confirmed_sale.click('confirm')
        self.assertIn(confirmed_sale.state,
            {'confirmed', 'processing', 'done'})
        draft_sale = create_sale(5)
        self.assertEqual(draft_sale.state, 'draft')
        quoted_sale = create_sale(7)
        quoted_sale.click('quote')
        cancelled_sale = create_sale(40)
        cancelled_sale.click('cancel')

        wizard = Wizard('sales.forecast.sale_history.update')
        self.assertIsNone(wizard.form.last_calculated_at)
        wizard.execute('update')

        history, = History.find([
                ('company', '=', company.id),
                ('product_template', '=', template.id),
                ('date', '=', date.today()),
                ])
        self.assertEqual(history.quantity, 24.0)
        wizard = Wizard('sales.forecast.sale_history.update')
        self.assertEqual(wizard.form.last_date, history.date)
        self.assertEqual(wizard.form.last_calculated_at, history.computed_at)
        wizard.execute('end')

        # Backdated sales require an explicit full rebuild.
        backdated = create_sale(3)
        backdated.sale_date = date.today() - timedelta(days=30)
        backdated.shipping_date = backdated.sale_date
        backdated.save()
        wizard = Wizard('sales.forecast.sale_history.update')
        self.assertFalse(wizard.form.full_history)
        wizard.execute('update')
        self.assertFalse(History.find([
                ('company', '=', company.id),
                ('date', '=', backdated.sale_date),
                ]))
        wizard = Wizard('sales.forecast.sale_history.update')
        wizard.form.full_history = True
        wizard.execute('update')
        old_history, = History.find([
                ('company', '=', company.id),
                ('date', '=', date.today() - timedelta(days=30)),
                ])
        self.assertEqual(old_history.quantity, 3)
        old_history_id = old_history.id
        old_computed_at = old_history.computed_at

        forecast = Forecast(
            name='Non-Cancelled Orders', company=company,
            calculation_method='key_accounts',
            start_date=date.today() - timedelta(days=date.today().weekday()),
            end_date=date.today() + timedelta(days=6-date.today().weekday()))
        forecast.product_templates.append(template)
        forecast.save()
        forecast.click('calculate')
        forecast.reload()
        self.assertEqual(forecast.lines[0].base_quantity, 24)
        self.assertEqual(forecast.lines[0].current_year_quantity, 24)

        # Cancelling an order removes its quantity from the refreshed history.
        draft_sale.click('cancel')
        Wizard('sales.forecast.sale_history.update').execute('update')
        forecast.click('calculate')
        forecast.reload()
        self.assertEqual(forecast.lines[0].base_quantity, 19)
        history, = History.find([
                ('company', '=', company.id),
                ('date', '=', date.today()),
                ])
        self.assertEqual(history.quantity, 19)

        # Confirming an already included quotation must not count it twice.
        quoted_sale.click('confirm')
        Wizard('sales.forecast.sale_history.update').execute('update')
        history, = History.find([
                ('company', '=', company.id),
                ('date', '=', date.today()),
                ])
        self.assertEqual(history.quantity, 19)

        # The next day must rebuild yesterday and include today's sales.
        later_sale = create_sale(4)
        later_sale.click('quote')
        later_sale.click('confirm')
        tomorrow = date.today() + timedelta(days=1)
        next_day_sale = create_sale(6)
        next_day_sale.sale_date = tomorrow
        next_day_sale.shipping_date = tomorrow
        next_day_sale.save()
        next_day_sale.click('quote')
        next_day_sale.click('confirm')
        with patch.object(config.pool.get('ir.date'), 'today',
                return_value=tomorrow):
            for _ in range(2):
                Wizard('sales.forecast.sale_history.update').execute('update')
                history, = History.find([
                        ('company', '=', company.id),
                        ('date', '=', date.today()),
                        ])
                self.assertEqual(history.quantity, 23)
                next_history, = History.find([
                        ('company', '=', company.id),
                        ('date', '=', tomorrow),
                        ])
                self.assertEqual(next_history.quantity, 6)
            wizard = Wizard('sales.forecast.sale_history.update')
            self.assertEqual(wizard.form.last_date, tomorrow)
            self.assertEqual(wizard.form.last_calculated_at,
                next_history.computed_at)
            wizard.execute('end')
        old_history.reload()
        self.assertEqual(old_history.id, old_history_id)
        self.assertEqual(old_history.computed_at, old_computed_at)
        self.assertEqual(old_history.quantity, 3)

        # A full rebuild also removes cancelled sales from earlier dates.
        backdated.click('cancel')
        with patch.object(config.pool.get('ir.date'), 'today',
                return_value=tomorrow):
            Wizard('sales.forecast.sale_history.update').execute('update')
            old_history.reload()
            self.assertEqual(old_history.quantity, 3)
            wizard = Wizard('sales.forecast.sale_history.update')
            wizard.form.full_history = True
            wizard.execute('update')
        self.assertFalse(History.find([
                ('company', '=', company.id),
                ('date', '=', backdated.sale_date),
                ]))
        history, = History.find([
                ('company', '=', company.id),
                ('date', '=', date.today()),
                ])
        self.assertEqual(history.quantity, 23)

        # Future orders count in every non-cancelled state, using delivery date.
        future_week = date.today() + timedelta(days=7-date.today().weekday())
        future_draft = create_sale(5, future_week)
        future_quote = create_sale(7, future_week)
        future_quote.click('quote')
        future_confirmed = create_sale(12, future_week)
        future_confirmed.sale_date = date.today()
        future_confirmed.save()
        future_confirmed.click('quote')
        future_confirmed.click('confirm')
        self.assertIn(future_confirmed.state,
            {'confirmed', 'processing', 'done'})
        future_cancelled = create_sale(40, future_week)
        future_cancelled.click('cancel')
        create_sale(100, future_week + timedelta(days=7))
        no_shipping_date = create_sale(3, future_week)
        no_shipping_date.shipping_date = None
        no_shipping_date.save()
        future_forecast = Forecast(name='Future Non-Cancelled Orders',
            company=company, calculation_method='key_accounts',
            start_date=future_week, end_date=future_week + timedelta(days=6))
        future_forecast.product_templates.append(ProductTemplate(template.id))
        future_forecast.save()
        future_forecast.click('calculate')
        future_forecast.reload()
        self.assertEqual(future_forecast.lines[0].base_quantity, 27)
        future_quote.click('confirm')
        future_forecast.click('calculate')
        future_forecast.reload()
        self.assertEqual(future_forecast.lines[0].base_quantity, 27)
        future_draft.click('cancel')
        future_forecast.click('calculate')
        future_forecast.reload()
        self.assertEqual(future_forecast.lines[0].base_quantity, 22)
        template.sale_forecast_key_account_source = 'mixed'
        template.save()
        future_forecast.click('calculate')
        future_forecast.reload()
        self.assertEqual(future_forecast.lines[0].base_quantity, 22)

        # Planning trends use closed periods from the refreshed history.
        trend_today = date(date.today().year + 3, 11, 9)
        with patch.object(config.pool.get('ir.date'), 'today',
                return_value=trend_today):
            template.reload()
            self.assertIsNone(template.sale_forecast_previous_year_growth)
            self.assertIsNone(template.sale_forecast_previous_month_growth)
            create_sale(100, date(trend_today.year - 2, 1, 1))
            create_sale(150, date(trend_today.year - 1, 12, 31))
            create_sale(960, date(trend_today.year - 1, 1, 1))
            create_sale(40, date(trend_today.year - 1, 10, 31))
            create_sale(5000, trend_today)
            create_sale(100, date(trend_today.year, 9, 1))
            last_month_sale = create_sale(80, date(trend_today.year, 10, 31))
            create_sale(999, date(trend_today.year, 11, 1))
            excluded = create_sale(999, date(trend_today.year, 10, 1))
            excluded.click('cancel')
            wizard = Wizard('sales.forecast.sale_history.update')
            wizard.form.full_history = True
            wizard.execute('update')
            template.reload()
            self.assertAlmostEqual(
                template.sale_forecast_previous_year_growth, .179)
            self.assertAlmostEqual(
                template.sale_forecast_previous_month_growth, 1)
            with config.set_context(company=None):
                other_context = ProductTemplate(template.id)
                self.assertIsNone(
                    other_context.sale_forecast_previous_year_growth)
                self.assertIsNone(
                    other_context.sale_forecast_previous_month_growth)
            last_month_sale.click('cancel')
            wizard = Wizard('sales.forecast.sale_history.update')
            wizard.form.full_history = True
            wizard.execute('update')
            template.reload()
            self.assertEqual(template.sale_forecast_previous_month_growth, -1)

            Calculation = Model.get('sales.forecast.calculation')
            calculation = Calculation(1)
            calculation.company = company
            calculation.start_date = date(trend_today.year, 1, 1)
            calculation.end_date = date(trend_today.year, 12, 31)
            calculation.save()
            self.assertAlmostEqual(calculation.previous_year_growth, .099)
            self.assertEqual(calculation.previous_month_growth, -1)
            self.assertIsNone(calculation.selected_period_growth)
            calculation.start_date = date(trend_today.year, 11, 9)
            reference_start = date(trend_today.year - 1, 11, 9)
            accumulated_days = (
                reference_start - date(trend_today.year - 1, 1, 1)).days
            expected_daily_growth = (150 / 53) / (1000 / accumulated_days) - 1
            self.assertAlmostEqual(calculation.selected_period_growth, expected_daily_growth)
            calculation.product_templates.append(ProductTemplate(template.id))
            self.assertAlmostEqual(
                calculation.selected_period_growth, expected_daily_growth)
            calculation.end_date = date(trend_today.year, 12, 30)
            self.assertEqual(calculation.selected_period_growth, -1)
            calculation.end_date = date(trend_today.year, 12, 31)
            without_history = ProductTemplate(
                name='No History Trend Product', default_uom=unit,
                sale_uom=unit, salable=True, type='goods',
                cost_price_method='fixed')
            without_history.save()
            calculation.product_templates.clear()
            calculation.product_templates.append(without_history)
            self.assertIsNone(calculation.previous_year_growth)
            self.assertIsNone(calculation.previous_month_growth)
            self.assertIsNone(calculation.selected_period_growth)

            # Increase lines show scoped trends for decision-making.
            Category = Model.get('product.category')
            root = Category(name='Trend Family')
            root.save()
            child = Category(name='Trend Subfamily', parent=root)
            child.save()
            template.reload()
            template.sale_forecast_method = 'general'
            template.categories.append(child)
            template.save()
            calculation.product_templates.clear()
            calculation.product_templates.append(ProductTemplate(template.id))
            calculation.product_increase_lines.new(
                product_template=ProductTemplate(template.id), percentage=.1)
            calculation.save()
            calculation.reload()
            product_trend, = calculation.product_increase_lines
            category_trend, = [line for line in calculation.category_increase_lines
                if line.category == child]
            for line in (product_trend, category_trend):
                self.assertAlmostEqual(line.previous_year_growth, .099)
                self.assertEqual(line.previous_month_growth, -1)
                self.assertAlmostEqual(line.selected_period_growth, expected_daily_growth)
            calculation.category_increase_lines.new(category=root)
            calculation.save()
            calculation.reload()
            root_trend, = [line for line in calculation.category_increase_lines
                if line.category == root]
            self.assertAlmostEqual(root_trend.selected_period_growth, expected_daily_growth)
            calculation.start_date = date(trend_today.year, 1, 1)
            calculation.save()
            calculation.reload()
            self.assertIsNone(
                calculation.product_increase_lines[0].selected_period_growth)
            self.assertEqual(calculation.product_increase_lines[0].percentage, .1)
