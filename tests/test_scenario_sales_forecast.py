import unittest
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from proteus import Model, Wizard
from trytond.tests.test_tryton import drop_db
from trytond.tests.tools import activate_modules
from trytond.modules.company.tests.tools import create_company, get_company


class TestSalesForecast(unittest.TestCase):

    def setUp(self):
        drop_db()
        super().setUp()

    def tearDown(self):
        drop_db()
        super().tearDown()

    def test(self):
        activate_modules(['sales_forecast', 'product_package'])

        ProductUom = Model.get('product.uom')
        ProductTemplate = Model.get('product.template')
        Forecast = Model.get('sales.forecast')
        ManualLine = Model.get('sales.forecast.manual_line')
        Calendar = Model.get('sales.forecast.calendar')
        Distribution = Model.get('sales.forecast.distribution')
        EventResult = Model.get('sales.forecast.event_result')
        DailyLine = Model.get('sales.forecast.daily_line')
        MonthlyLine = Model.get('sales.forecast.monthly_line')
        SaleHistory = Model.get('sales.forecast.sale_history')
        Party = Model.get('party.party')
        ProductCategory = Model.get('product.category')
        Sale = Model.get('sale.sale')
        StockLocation = Model.get('stock.location')

        create_company()
        company = get_company()
        calendars = Calendar.find([])
        self.assertEqual(len(calendars), 1)
        self.assertEqual(calendars[0].name, 'Calendario Base Previsiones')
        unit, = ProductUom.find([('name', '=', 'Unit')])
        warehouse, = StockLocation.find([('code', '=', 'WH')])
        template = ProductTemplate(
            name='Forecast Product',
            default_uom=unit,
            sale_uom=unit,
            salable=True,
            type='goods',
            cost_price_method='fixed')
        template.sales_forecast_method = 'manual'
        special_category = ProductCategory(name='Special Category')
        template.categories.append(special_category)
        package = template.packages.new()
        package.name = 'Box'
        package.quantity = 10
        package.is_default = True
        template.save()
        with patch(
                'trytond.modules.sales_forecast.forecast.'
                'SalesForecastManualLine._current_week_start',
                return_value=date(2026, 8, 3)):
            manual_line = ManualLine(
                product_template=template,
                week_start=date(2026, 8, 3),
                units=100)
            manual_line.save()
            manual_line = ManualLine(
                product_template=template,
                week_start=date(2026, 8, 10),
                units=100)
            manual_line.save()

        general_template = ProductTemplate(
            name='General Product',
            default_uom=unit,
            sale_uom=unit,
            salable=True,
            type='goods',
            cost_price_method='fixed')
        general_template.sales_forecast_method = 'general'
        general_template.save()

        key_orders_template = ProductTemplate(
            name='Key Orders Product',
            default_uom=unit,
            sale_uom=unit,
            salable=True,
            type='goods',
            cost_price_method='fixed')
        key_orders_template.sales_forecast_method = 'key_accounts'
        key_orders_template.sales_forecast_key_account_source = 'orders'
        key_orders_template.save()

        key_mixed_template = ProductTemplate(
            name='Key Mixed Product',
            default_uom=unit,
            sale_uom=unit,
            salable=True,
            type='goods',
            cost_price_method='fixed')
        key_mixed_template.sales_forecast_method = 'key_accounts'
        key_mixed_template.sales_forecast_key_account_source = 'mixed'
        key_mixed_template.save()

        def fake_aggregated_sale_rows(
                history_company, start_date=None, end_date=None):
            self.assertEqual(history_company.id, company.id)
            if start_date is None:
                return [
                    (template.id, date(2025, 8, 4), 10.0),
                    (template.id, date(2025, 8, 5), 20.0),
                    (template.id, date(2025, 8, 6), 30.0),
                    (template.id, date(2025, 8, 7), 40.0),
                    (template.id, date(2025, 8, 8), 50.0),
                    (general_template.id, date(2025, 8, 4), 50.0),
                    (general_template.id, date(2025, 8, 11), 70.0),
                    (general_template.id, date(2026, 8, 4), 12.0),
                ]

        with patch(
                'trytond.modules.sales_forecast.forecast.'
                'SalesForecastSaleHistory._aggregated_sale_rows',
                side_effect=fake_aggregated_sale_rows):
            wizard = Wizard('sales.forecast.sale_history.update')
            wizard.execute('update')
            wizard = Wizard('sales.forecast.sale_history.update')
            wizard.execute('update')
        history_rows = SaleHistory.find([
            ('company', '=', company.id),
            ('product_template', '=', general_template.id),
        ])
        self.assertEqual(len(history_rows), 3)
        history_quantities = {
            history.date: history.quantity for history in history_rows
        }
        self.assertEqual(history_quantities[date(2025, 8, 4)], 50.0)
        self.assertEqual(history_quantities[date(2025, 8, 11)], 70.0)
        self.assertEqual(history_quantities[date(2026, 8, 4)], 12.0)
        history_patch = patch(
            'trytond.modules.sales_forecast.forecast.'
            'SalesForecastSaleHistory._aggregated_sale_rows',
            side_effect=fake_aggregated_sale_rows)
        history_patch.start()
        self.addCleanup(history_patch.stop)

        calendar = Calendar(name='Planning Calendar')
        day = calendar.day_lines.new()
        day.date = date(2026, 8, 4)
        day.day_type = 'special_sale'
        special = day.special_lines.new()
        special.scope = 'product_category'
        special.product_category = special_category
        special.increase_percentage = 0.2
        product_day = calendar.day_lines.new()
        product_day.date = date(2026, 8, 5)
        product_day.day_type = 'product_day'
        product_line = product_day.product_lines.new()
        product_line.product_template = template
        product_line.units = 200
        calendar.save()

        party = Party(name='Forecast Customer')
        address = party.addresses.new()
        address.street = 'Main Street'
        address.city = 'Barcelona'
        party.save()

        sale = Sale(
            party=party,
            invoice_party=party,
            shipment_party=party,
            warehouse=warehouse,
            company=company,
            currency=company.currency,
            sale_date=date(2026, 8, 4),
            shipping_date=date(2026, 8, 5))
        line = sale.lines.new()
        line.product = key_orders_template.products[0]
        line.quantity = 30
        line.unit = unit
        line.unit_price = Decimal('1')
        line = sale.lines.new()
        line.product = key_mixed_template.products[0]
        line.quantity = 25
        line.unit = unit
        line.unit_price = Decimal('1')
        sale.save()

        forecast = Forecast(
            name='August Forecast',
            company=company,
            calendar=calendar,
            general_increase_percentage=0.1,
            start_date=date(2026, 8, 3),
            end_date=date(2026, 8, 16))
        product_increase = forecast.product_increase_lines.new()
        product_increase.product_template = general_template
        product_increase.percentage = 0.2

        product_ids = {
            general_template.id: {
                date(2026, 8, 3): 50.0,
                date(2026, 8, 10): 70.0,
            },
            key_mixed_template.id: {
                date(2026, 8, 3): 10.0,
                date(2026, 8, 10): 40.0,
            },
        }

        def fake_previous_year_quantity(
                history_company, template, week_start):
            self.assertEqual(history_company.id, company.id)
            return product_ids.get(template.id, {}).get(week_start, 0.0)

        def fake_distribution_quantities(
                history_company, template, excluded_dates=None):
            self.assertEqual(history_company.id, company.id)
            values = {
                template.id: {
                    '0': 10.0,
                    '1': 20.0,
                    '2': 10.0,
                    '3': 20.0,
                    '4': 20.0,
                    '5': 10.0,
                    '6': 10.0,
                }
            }
            return values.get(template.id, {str(i): 0.0 for i in range(7)})

        distribution_patch = patch(
            'trytond.modules.sales_forecast.forecast.'
            'SalesForecastCalendar._distribution_quantities',
            side_effect=fake_distribution_quantities)
        distribution_patch.start()
        self.addCleanup(distribution_patch.stop)

        with patch(
                'trytond.modules.sales_forecast.forecast.'
                'SalesForecastCalendar._distribution_quantities',
                side_effect=fake_distribution_quantities):
            calendar.click('calculate_distributions')
        calendar.reload()
        self.assertIsNotNone(calendar.distribution_calculated_at)
        distributions = Distribution.find([
            ('product_template', '=', template.id),
        ])
        self.assertEqual(len(distributions), 7)
        self.assertEqual(sum(round(d.percentage, 2) for d in distributions), 100.0)
        self.assertEqual(
            [distribution.sequence for distribution in distributions],
            list(range(7)))
        self.assertEqual(
            [distribution.weekday for distribution in distributions],
            [str(i) for i in range(7)])

        calendar.click('calculate_event_results')
        calendar.reload()
        self.assertIsNotNone(calendar.events_calculated_at)
        results = EventResult.find([])
        self.assertEqual(len(results), 1)
        product_day_result, = EventResult.find([
            ('calendar_day.date', '=', date(2026, 8, 5)),
            ('product_template', '=', template.id),
        ])
        self.assertEqual(product_day_result.units, 200.0)
        self.assertEqual(product_day_result.boxes, 20.0)
        self.assertIsNone(product_day_result.pallets)

        with patch(
                'trytond.modules.sales_forecast.forecast.'
                'SalesForecast._previous_year_week_quantity',
                side_effect=fake_previous_year_quantity):
            forecast.calculation_method = 'manual'
            forecast.click('calculate')
            forecast.click('cancel')
            forecast.click('draft')
            forecast.calculation_method = 'general'
            forecast.click('calculate')
            forecast.click('cancel')
            forecast.click('draft')
            forecast.calculation_method = 'key_accounts'
            forecast.click('calculate')
            forecast.reload()

        self.assertEqual(forecast.state, 'calculated')
        self.assertEqual(len(forecast.lines), 8)
        first_computed_at = forecast.computed_at

        lines = {
            (line.product_template.name, line.week_start): line.quantity
            for line in forecast.lines
        }
        self.assertEqual(lines[('Forecast Product', date(2026, 8, 3))], 100.0)
        self.assertEqual(lines[('Forecast Product', date(2026, 8, 10))], 100.0)
        self.assertEqual(lines[('General Product', date(2026, 8, 3))], 60.0)
        self.assertEqual(lines[('General Product', date(2026, 8, 10))], 84.0)
        self.assertEqual(lines[('Key Orders Product', date(2026, 8, 3))], 30.0)
        self.assertEqual(lines[('Key Orders Product', date(2026, 8, 10))], 0.0)
        self.assertEqual(lines[('Key Mixed Product', date(2026, 8, 3))], 25.0)
        self.assertEqual(lines[('Key Mixed Product', date(2026, 8, 10))], 44.0)
        forecast_line, = [line for line in forecast.lines
            if line.product_template.name == 'Forecast Product'
            and line.week_start == date(2026, 8, 10)]
        self.assertEqual(forecast_line.units, 100.0)
        self.assertEqual(forecast_line.boxes, 10.0)
        self.assertIsNone(forecast_line.pallets)
        general_forecast_line, = [line for line in forecast.lines
            if line.product_template.name == 'General Product'
            and line.week_start == date(2026, 8, 3)]
        self.assertEqual(general_forecast_line.current_year_quantity, 12.0)
        self.assertEqual(general_forecast_line.previous_year_1_quantity, 50.0)
        self.assertEqual(general_forecast_line.previous_year_2_quantity, 0.0)
        self.assertEqual(general_forecast_line.previous_year_3_quantity, 0.0)
        daily_lines = DailyLine.find([
            ('forecast', '=', forecast.id),
        ])
        self.assertEqual(len(daily_lines), 56)
        monthly_lines = MonthlyLine.find([
            ('forecast', '=', forecast.id),
        ])
        self.assertEqual(len(monthly_lines), 4)
        forecast_daily_lines = DailyLine.find([
            ('forecast', '=', forecast.id),
            ('product_template', '=', template.id),
            ('date', '=', date(2026, 8, 4)),
        ])
        forecast_daily_line, = forecast_daily_lines
        self.assertEqual(forecast_daily_line.quantity, 20.0)
        self.assertEqual(forecast_daily_line.previous_year_1_quantity, 20.0)

        general_daily_lines = DailyLine.find([
            ('forecast', '=', forecast.id),
            ('product_template', '=', general_template.id),
            ('date', '=', date(2026, 8, 4)),
        ])
        general_daily_line, = general_daily_lines
        self.assertEqual(general_daily_line.current_year_quantity, 12.0)
        self.assertEqual(general_daily_line.previous_year_1_quantity, 0.0)
        general_monthly_lines = MonthlyLine.find([
            ('forecast', '=', forecast.id),
            ('product_template', '=', general_template.id),
            ('month_start', '=', date(2026, 8, 1)),
        ])
        general_monthly_line, = general_monthly_lines
        self.assertEqual(general_monthly_line.quantity, 144.0)
        self.assertEqual(general_monthly_line.current_year_quantity, 12.0)
        self.assertEqual(general_monthly_line.previous_year_1_quantity, 120.0)

        with patch(
                'trytond.modules.sales_forecast.forecast.'
                'SalesForecast._previous_year_week_quantity',
                side_effect=fake_previous_year_quantity):
            forecast.click('cancel')
            forecast.click('draft')
            forecast.calculation_method = 'manual'
            forecast.click('calculate')
            forecast.click('cancel')
            forecast.click('draft')
            forecast.calculation_method = 'general'
            forecast.click('calculate')
            forecast.click('cancel')
            forecast.click('draft')
            forecast.calculation_method = 'key_accounts'
            forecast.click('calculate')
            forecast.reload()
        self.assertEqual(forecast.state, 'calculated')
        self.assertTrue(forecast.computed_at >= first_computed_at)
        self.assertEqual(len(forecast.lines), 8)

        def fake_actual_current_year_rows(
                history_company, start_date=None, end_date=None):
            self.assertEqual(history_company.id, company.id)
            self.assertIsNone(start_date)
            return [(template_id, day, 18.0 if day == date(2026, 8, 4)
                    else quantity)
                for template_id, day, quantity in fake_aggregated_sale_rows(
                    history_company)]

        with patch(
                'trytond.modules.sales_forecast.forecast.'
                'SalesForecastSaleHistory._aggregated_sale_rows',
                side_effect=fake_actual_current_year_rows):
            forecast.click('update_current_year_sales')
        forecast.reload()
        general_forecast_line, = [line for line in forecast.lines
            if line.product_template.name == 'General Product'
            and line.week_start == date(2026, 8, 3)]
        self.assertEqual(general_forecast_line.current_year_quantity, 18.0)
        general_daily_lines = DailyLine.find([
            ('forecast', '=', forecast.id),
            ('product_template', '=', general_template.id),
            ('date', '=', date(2026, 8, 4)),
        ])
        general_daily_line, = general_daily_lines
        self.assertEqual(general_daily_line.current_year_quantity, 18.0)
        general_monthly_lines = MonthlyLine.find([
            ('forecast', '=', forecast.id),
            ('product_template', '=', general_template.id),
            ('month_start', '=', date(2026, 8, 1)),
        ])
        general_monthly_line, = general_monthly_lines
        self.assertEqual(general_monthly_line.current_year_quantity, 18.0)

        forecast.click('cancel')
        forecast.click('draft')
        forecast.product_templates.append(general_template)
        forecast.calculation_method = 'general'
        forecast.save()

        with patch(
                'trytond.modules.sales_forecast.forecast.'
                'SalesForecast._previous_year_week_quantity',
                side_effect=fake_previous_year_quantity):
            forecast.click('calculate')
        forecast.reload()
        self.assertIsNotNone(forecast.last_general_calculated_at)
        generated_forecast = forecast
        self.assertEqual(generated_forecast.state, 'calculated')
        general_lines = [line for line in generated_forecast.lines
            if line.method == 'general']
        self.assertEqual(len(general_lines), 2)
        self.assertEqual({
                line.product_template.id for line in general_lines
                }, {general_template.id})
